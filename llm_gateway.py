"""Ollama互換の生成窓口: 他のアプリが ai-agent-lab の最適化を通してローカルLLMを1回呼ぶ。

GET /api/tags と POST /api/generate（stream=false）だけを Ollama と同じ形で受け、
llm.chat に渡す。models.yaml の options・num_ctx・think・keep_alive と、
GPU同時実行の交通整理（_gate）がそのまま効く。呼び出し側の options.temperature は
判定・JSON用途の明示として尊重する（README「判定系だけ呼び出し側が低温度を明示」）。

server.py とは別プロセスにして、エージェント実行（/run 等）を外へ出さない。
使えるのは models.yaml の Ollama バックエンドのモデルだけ（外部APIへは流さない）。

起動: python llm_gateway.py  →  http://127.0.0.1:8766
  GATEWAY_HOST  待ち受け先（Tailscale から使うなら このPCの Tailscale IP）
  GATEWAY_PORT  既定 8766
  GATEWAY_TOKEN 設定すると Authorization: Bearer <token> を必須にする
"""
import hmac
import os

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import llm

app = FastAPI(title="ai-agent-lab LLM gateway")


def _token() -> str:
    return os.environ.get("GATEWAY_TOKEN", "")


def _unauthorized(request: Request):
    token = _token()
    if not token:
        return None
    given = request.headers.get("authorization", "").removeprefix("Bearer ")
    if hmac.compare_digest(given.encode(), token.encode()):
        return None
    return JSONResponse({"error": "unauthorized"}, status_code=401)


def _local_models(cfg) -> dict:
    return {k: m for k, m in (cfg.get("models") or {}).items()
            if m.get("backend", "ollama") == "ollama" and m.get("tag")}


def _key_for(cfg, name: str):
    """モデル名（models.yaml のキー or Ollamaタグ）→ キー。ローカル以外・未定義は None。"""
    models = _local_models(cfg)
    if name in models:
        return name
    for key, m in models.items():
        if name in (m["tag"], f"{m['tag']}:latest"):
            return key
    return None


@app.get("/api/tags")
async def tags(request: Request):
    if denied := _unauthorized(request):
        return denied
    names = []
    for key, m in _local_models(llm.load_config()).items():
        names += [m["tag"], key]
    return {"models": [{"name": n} for n in dict.fromkeys(names)]}


@app.post("/api/generate")
async def generate(request: Request):
    if denied := _unauthorized(request):
        return denied
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("options") or {}, dict):
        return JSONResponse({"error": "本文は JSON オブジェクト（options もオブジェクト）で送ってください"},
                            status_code=400)
    cfg = llm.load_config()
    key = _key_for(cfg, str(body.get("model", "")))
    if key is None:
        return JSONResponse({"error": f"models.yaml にローカルモデル {body.get('model')!r} がありません"},
                            status_code=404)
    info = llm.resolve(cfg, key)
    prompt = body.get("prompt") or ""
    if not prompt:
        # Ollama と同じく、プロンプト無しはモデルの読み込みだけ（事前ウォームアップ用）
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=llm.CONNECT_TIMEOUT)) as c:
                r = await c.post(f"{llm.OLLAMA_BASE}/api/generate", json={
                    "model": info["tag"],
                    "keep_alive": info["keep_alive"] if body.get("keep_alive") is None else body["keep_alive"]})
            r.raise_for_status()
        except httpx.HTTPError as e:
            return JSONResponse({"error": f"モデルを読み込めません: {e}"}, status_code=502)
        return {"model": body.get("model"), "response": "", "done": True}
    fmt = body.get("format")
    try:
        msg = await llm.chat(
            cfg, key, [{"role": "user", "content": prompt}],
            temperature=(body.get("options") or {}).get("temperature"),
            json_mode=fmt == "json",
            json_schema=fmt if isinstance(fmt, dict) else None,
        )
    except llm.OllamaError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return {"model": body.get("model"), "response": msg.get("content", ""), "done": True}


if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("GATEWAY_HOST", "127.0.0.1"),
                port=int(os.environ.get("GATEWAY_PORT", "8766")), log_level="warning")

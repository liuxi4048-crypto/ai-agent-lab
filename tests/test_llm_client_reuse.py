import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm


CFG = {"models": {"remote": {"tag": "served", "backend": "openai",
       "base_url": "http://localhost:8080/v1/", "tools": True,
       "options": {"temperature": 0.6}}}}


def _sse():
    chunks = [{"choices": [{"delta": {"content": "ok"}}]},
              {"choices": [{"delta": {}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 3, "completion_tokens": 1}}]
    return ''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n'


def test_client_reused_within_loop_and_recreated_per_loop(monkeypatch):
    created = []
    real = httpx.AsyncClient

    def factory(**kw):
        created.append(kw)
        return real(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=_sse())), **kw)

    monkeypatch.setattr(llm.httpx, "AsyncClient", factory)

    async def two_chats():
        a = await llm.chat(CFG, "remote", [{"role": "user", "content": "hi"}])
        b = await llm.chat(CFG, "remote", [{"role": "user", "content": "hi"}])
        return a, b

    a, b = asyncio.run(two_chats())
    assert a["content"] == b["content"] == "ok"
    assert len(created) == 1          # 同じループ内では接続を使い回す

    asyncio.run(two_chats())
    assert len(created) == 2          # ループが変われば作り直す(束縛回避)

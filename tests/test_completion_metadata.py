import asyncio
import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import llm


@pytest.mark.parametrize("reason",["stop","length",None])
def test_completion_marker(monkeypatch,reason):
    chunks=[{"message":{"content":"reply"}}]
    if reason:
        chunks.append({"done":True,"done_reason":reason})
    class Response:
        status_code=200
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def aiter_lines(self):
            for chunk in chunks:
                yield json.dumps(chunk)
    class Client:
        def __init__(self,**kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        def stream(self,*args,**kw): return Response()
    monkeypatch.setattr(llm.httpx,"AsyncClient",Client)
    message=asyncio.run(llm._stream_collect({},1,None))
    assert message["content"] == "reply"
    assert message.get("_done_reason") == reason

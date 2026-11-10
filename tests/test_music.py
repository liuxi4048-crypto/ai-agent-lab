import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm
import music
import router


def mock_client(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(music.httpx, "AsyncClient", lambda **kw: original(
        transport=httpx.MockTransport(handler), **kw))


def wrapped(data):
    return httpx.Response(200, json={"data": data, "code": 200, "error": None})


@pytest.mark.parametrize("seed", [None, 0, 42])
def test_generate_payload_and_save(monkeypatch, tmp_path, seed):
    monkeypatch.setattr(music, "MUSIC_OUT", tmp_path)
    calls = []
    async def sleep(seconds):
        calls.append(seconds)
    monkeypatch.setattr(music.asyncio, "sleep", sleep)
    polls = []
    def handler(request):
        if request.url.path == "/release_task":
            p = json.loads(request.content)
            assert p == {"prompt": "jazz", "lyrics": "[Instrumental]",
                         "model": "acestep-v15-turbo", "thinking": True,
                         "vocal_language": "ja", "audio_duration": 30,
                         "inference_steps": 8, "batch_size": 1, "audio_format": "mp3",
                         "use_format": False, "use_random_seed": seed is None,
                         "lm_model_path": "acestep-5Hz-lm-1.7B", "lm_backend": "pt",
                         **({"seed": seed} if seed is not None else {})}
            return wrapped({"task_id": "task"})
        if request.url.path == "/query_result":
            assert json.loads(request.content) == {"task_id_list": ["task"]}
            polls.append(1)
            return wrapped([{"task_id": "task", "status": 0 if len(polls) == 1 else 1,
                             "result": json.dumps([{"file": "/v1/audio?path=%2Ftmp%2Fa.mp3",
                                                    "metas": {"duration": 30}, "seed_value": "42"}])}])
        assert request.url.path == "/v1/audio"
        assert request.url.params["path"] == "/tmp/a.mp3"
        return httpx.Response(200, content=b"mp3 audio")
    mock_client(monkeypatch, handler)
    result = asyncio.run(music.generate_music(llm.load_config(), "music", "jazz",
                                             duration=30, seed=seed, instrumental=True))
    assert Path(result["path"]).read_bytes() == b"mp3 audio"
    assert result["file"].endswith(".mp3")
    assert result["duration"] == 30 and result["seed"] == "42"
    assert result["elapsed_sec"] >= 0
    assert calls == [2]


@pytest.mark.parametrize("failure", ["failed", "connect", "timeout", "wrapper", "malformed"])
def test_errors(monkeypatch, failure):
    def handler(request):
        if failure == "connect":
            raise httpx.ConnectError("offline", request=request)
        if failure == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if failure == "wrapper":
            return httpx.Response(200, json={"data": None, "code": 500, "error": "GPU error"})
        if failure == "malformed":
            return wrapped({})
        if request.url.path == "/release_task":
            return wrapped({"task_id": "task"})
        return wrapped([{"task_id": "task", "status": 2, "error": "GPU error"}])
    mock_client(monkeypatch, handler)
    expected = {"connect": "接続できません", "timeout": "タイムアウト", "failed": "失敗",
                "wrapper": "エラー", "malformed": "応答形式"}[failure]
    with pytest.raises(music.MusicError, match=expected):
        asyncio.run(music.generate_music(llm.load_config(), "music", "jazz"))
    assert not music._generation_lock.locked()


def test_catalog_cached(monkeypatch):
    music._health_cache.clear()
    requests = []
    def handler(request):
        requests.append(request)
        return wrapped({"status": "ok"})
    mock_client(monkeypatch, handler)
    cfg = llm.load_config()
    for _ in range(2):
        result = asyncio.run(music.music_catalog(cfg))
        assert {m["key"] for m in result["models"]} == {"music", "music_xl"}
        assert all(m["connected"] for m in result["models"])
    assert len(requests) == 1
    music._health_cache.clear()


def test_config_and_router():
    cfg = llm.load_config()
    # gui.py / step1_chat.py は models.yaml を直接読み m['tag'] を参照する。
    # 音楽エントリにも tag が無いと GUI 起動時に KeyError になる（レビュー指摘）。
    for key, entry in cfg["models"].items():
        assert entry.get("tag"), f"{key} に tag がありません"
    for key in ("music", "music_xl"):
        assert cfg["models"][key]["kind"] == "music"
        assert cfg["models"][key]["tools"] is False
        assert llm.resolve(cfg, key)["kind"] == "music"
        for mode in ("direct", "critique", "orchestra", "code", "swarm-code"):
            assert not router.usable(cfg, key, mode)
    assert not {"music", "music_xl"} & {m["key"] for m in llm.model_catalog(cfg)["models"]}


@pytest.mark.parametrize("key", ["music", "music_xl"])
def test_chat_guard(key):
    cfg = llm.load_config()
    async def stream():
        return [event async for event in llm.chat_stream(cfg, key, [])]
    for call in (lambda: asyncio.run(llm.chat(cfg, key, [])),
                 lambda: llm.chat_sync(cfg, key, []), lambda: asyncio.run(stream())):
        with pytest.raises(llm.OllamaFatal, match="音楽生成モデルです。/music/generate を使ってください"):
            call()


def test_music_routes(monkeypatch, tmp_path):
    import server
    monkeypatch.setattr(music, "MUSIC_OUT", tmp_path)
    monkeypatch.setattr(server.openai_api, "_check_auth", lambda request: None)
    (tmp_path / "sample.mp3").write_bytes(b"audio")
    outside = tmp_path.parent / "outside.mp3"
    outside.write_bytes(b"secret")
    (tmp_path / "link.mp3").symlink_to(outside)
    original = httpx.AsyncClient
    async def run():
        async with original(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
            for name in ("..%2Foutside.mp3", "..%5Coutside.mp3", "%2Fetc%2Fpasswd", "link.mp3"):
                response = await client.get("/music/file/" + name)
                assert response.status_code == 400
            response = await server.music_file("sample.mp3", None)
            assert Path(response.path).read_bytes() == b"audio"
            assert (await client.get("/music/file/missing.mp3")).status_code == 404
            assert (await client.post("/music/generate", json={"model": "fast", "prompt": "jazz"})).status_code == 400
            async def generate(*args, **kwargs):
                return {"file": "sample.mp3", "path": str(tmp_path / "sample.mp3")}
            monkeypatch.setattr(music, "generate_music", generate)
            response = await client.post("/music/generate", json={"prompt": "jazz"})
            assert response.json()["url"] == "/music/file/sample.mp3"
            async def fail(*args, **kwargs):
                raise music.MusicError("接続できません")
            monkeypatch.setattr(music, "generate_music", fail)
            assert (await client.post("/music/generate", json={"prompt": "jazz"})).status_code == 502
    asyncio.run(run())


def test_catalog_offline_cached(monkeypatch):
    music._health_cache.clear()
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("offline", request=request)
    mock_client(monkeypatch, handler)
    for _ in range(2):
        result = asyncio.run(music.music_catalog(llm.load_config()))
        assert all(not entry["connected"] for entry in result["models"])
    assert len(calls) == 1
    music._health_cache.clear()


def test_lock_wait_timeout():
    music._generation_lock.acquire()
    try:
        with pytest.raises(music.MusicError, match="タイムアウト"):
            asyncio.run(music.generate_music(llm.load_config(), "music", "jazz", timeout=0.01))
        assert music._generation_lock.locked()
    finally:
        music._generation_lock.release()


def test_music_usable_reason():
    cfg = llm.load_config()
    for key in ("music", "music_xl"):
        usable, status = asyncio.run(llm.model_usable(cfg, key, set()))
        assert (usable, status) == (False, "music")
        assert llm.unusable_reason(llm.resolve(cfg, key), status) == \
            "音楽生成モデルです。/music/generate を使ってください"


def test_non_dict_response(monkeypatch):
    def handler(request):
        return httpx.Response(200, json=["not", "a", "dict"])
    mock_client(monkeypatch, handler)
    with pytest.raises(music.MusicError, match="応答形式"):
        asyncio.run(music.generate_music(llm.load_config(), "music", "jazz"))


def test_generation_serialized(monkeypatch, tmp_path):
    monkeypatch.setattr(music, "MUSIC_OUT", tmp_path)
    active = []
    async def handler(request):
        if request.url.path == "/release_task":
            assert not active
            active.append("task")
            await asyncio.sleep(0.01)
            return wrapped({"task_id": "task"})
        if request.url.path == "/query_result":
            return wrapped([{"task_id": "task", "status": 1,
                             "result": [{"file": "/tmp/a.mp3"}]}])
        active.pop()
        return httpx.Response(200, content=b"audio")
    mock_client(monkeypatch, handler)
    async def run():
        cfg = llm.load_config()
        return await asyncio.gather(*(music.generate_music(cfg, "music", "jazz") for _ in range(2)))
    results = asyncio.run(run())
    assert len({result["file"] for result in results}) == 2
    assert not music._generation_lock.locked()

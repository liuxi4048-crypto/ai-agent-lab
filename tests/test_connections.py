import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import connections
import llm
import llm_gateway
import server

CFG = {"models": {
    "fast": {"tag": "gemma3:12b"},
    "remote": {"tag": "served", "backend": "openai"},
}}


@pytest.fixture(autouse=True)
def lab(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("GATEWAY_TOKEN", raising=False)
    monkeypatch.setattr(llm, "load_config", lambda *a, **k: CFG)
    monkeypatch.setattr(connections.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": "100.1.2.3\n"})())
    connections._last_touch.clear()
    return tmp_path


def test_add_rotate_delete_match():
    c = connections.add("結婚式アプリ", "gemma3:12b")
    assert connections.match_token(c["token"])["id"] == c["id"]
    assert connections.match_token("nope") is None
    old = c["token"]
    new = connections.rotate(c["id"])
    assert new != old and connections.match_token(old) is None
    assert connections.match_token(new)["id"] == c["id"]
    assert connections.rotate("zzz") is None
    assert connections.delete(c["id"]) and connections.match_token(new) is None
    assert not connections.delete(c["id"])


def test_touch_is_throttled(monkeypatch):
    c = connections.add("a", "gemma3:12b")
    now = [1000.0]
    monkeypatch.setattr(connections.time, "time", lambda: now[0])
    connections.touch(c["id"], "100.9.9.9")
    now[0] += 10
    connections.touch(c["id"], "100.8.8.8")
    got = connections.load()["connections"][0]
    assert got["last_used_at"] == 1000.0 and got["last_client"] == "100.9.9.9"
    now[0] += 30
    connections.touch(c["id"], "100.8.8.8")
    assert connections.load()["connections"][0]["last_client"] == "100.8.8.8"


def test_code_round_trip():
    c = connections.add("a", "gemma3:12b")
    code = connections.connection_code(c, "100.1.2.3", 8766)
    assert code.startswith("aal://100.1.2.3:8766?model=gemma3%3A12b&key=")
    assert connections.parse_code(code) == {
        "host": "100.1.2.3", "port": 8766, "model": "gemma3:12b", "key": c["token"]}


def test_set_gateway_and_defaults():
    assert connections.load()["gateway"] == {"host": "", "port": 8766, "autostart": True}
    connections.set_gateway(port=9000, autostart=False)
    assert connections.load()["gateway"] == {"host": "", "port": 9000, "autostart": False}
    with pytest.raises(ValueError):
        connections.set_gateway(port=70000)


def test_default_host_order(monkeypatch):
    assert connections.default_host() == "100.1.2.3"
    monkeypatch.setattr(connections.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert connections.default_host() == "127.0.0.1"
    connections.set_gateway(host="100.7.7.7")
    assert connections.default_host() == "100.7.7.7"


def test_gateway_auth_with_saved_tokens(monkeypatch):
    gw = TestClient(llm_gateway.app)
    assert gw.get("/api/tags").status_code == 200          # 未設定は従来どおり開放
    c = connections.add("a", "gemma3:12b")
    assert gw.get("/api/tags").status_code == 401
    ok = gw.get("/api/tags", headers={"Authorization": f"Bearer {c['token']}"})
    assert ok.status_code == 200
    assert connections.load()["connections"][0]["last_client"] == "testclient"
    new = connections.rotate(c["id"])
    assert gw.get("/api/tags", headers={"Authorization": f"Bearer {c['token']}"}).status_code == 401
    assert gw.get("/api/tags", headers={"Authorization": f"Bearer {new}"}).status_code == 200
    monkeypatch.setenv("GATEWAY_TOKEN", "env-token")
    assert gw.get("/api/tags", headers={"Authorization": "Bearer env-token"}).status_code == 200
    assert gw.get("/api/tags", headers={"Authorization": f"Bearer {new}"}).status_code == 200
    assert gw.get("/api/tags").status_code == 401


class FakeProc:
    pid = 4321
    alive = True

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.alive = False

    def wait(self, timeout=None):
        pass


@pytest.fixture
def dash(monkeypatch):
    monkeypatch.setattr(server, "ROOT", connections._path().parent)
    server._gateway["proc"] = None
    monkeypatch.setattr(server.socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    yield TestClient(server.app)
    server._gateway["proc"] = None


def test_endpoints_never_leak_token_in_list(dash):
    r = dash.post("/connections", json={"name": " 結婚式 ", "model": "gemma3:12b"})
    assert r.status_code == 200
    made = r.json()
    token = connections.load()["connections"][0]["token"]
    assert made["name"] == "結婚式" and "token" not in made and token in made["code"]
    assert made["code"].startswith("aal://100.1.2.3:8766?model=gemma3%3A12b")
    listed = dash.get("/connections")
    assert token not in listed.text and listed.json()["connections"][0]["id"] == made["id"]
    assert dash.get(f"/connections/{made['id']}/code").json()["code"] == made["code"]
    code2 = dash.post(f"/connections/{made['id']}/rotate").json()["code"]
    assert code2 != made["code"] and connections.match_token(connections.parse_code(code2)["key"])
    assert dash.delete(f"/connections/{made['id']}").status_code == 200
    assert dash.delete(f"/connections/{made['id']}").status_code == 404
    assert dash.get("/connections/x/code").status_code == 404


def test_invalid_model_and_name_rejected(dash):
    assert dash.post("/connections", json={"name": "a", "model": "served"}).status_code == 400
    assert dash.post("/connections", json={"name": "a", "model": "nope"}).status_code == 400
    assert dash.post("/connections", json={"name": " ", "model": "fast"}).status_code == 400
    assert connections.load()["connections"] == []
    assert dash.post("/connections", json={"name": "a", "model": "fast"}).json()["model"] == "gemma3:12b"


def test_gateway_start_stop_idempotent(dash, monkeypatch):
    calls = []

    def fake_popen(cmd, **kw):
        calls.append((cmd, kw))
        return FakeProc()

    monkeypatch.setattr(server.subprocess, "Popen", fake_popen)
    assert dash.put("/gateway", json={"host": "127.0.0.1", "port": 9100, "autostart": False}).json()["port"] == 9100
    s = dash.post("/gateway/start").json()
    assert s["running"] and s["pid"] == 4321
    dash.post("/gateway/start")
    assert len(calls) == 1
    cmd, kw = calls[0]
    assert cmd == [sys.executable, "llm_gateway.py"] and kw["cwd"] == server.ROOT
    assert kw["env"]["GATEWAY_HOST"] == "127.0.0.1" and kw["env"]["GATEWAY_PORT"] == "9100"
    assert dash.post("/gateway/stop").json()["running"] is False
    assert dash.put("/gateway", json={"port": 0}).status_code == 400

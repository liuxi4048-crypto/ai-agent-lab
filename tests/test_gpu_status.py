import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm

GB = 1024 ** 3
MODELS = [{"name": "qwen:latest", "size_vram": 5 * GB}]


def _run(monkeypatch, models, driver_used):
    async def fake_models():
        return models

    async def fake_driver():
        return driver_used

    monkeypatch.setattr(llm, "_ollama_loaded_models", fake_models)
    monkeypatch.setattr(llm, "_driver_vram_used_bytes", fake_driver)
    return asyncio.run(llm.gpu_status())


def test_driver_measurement_wins_and_external_is_listed(monkeypatch):
    st = _run(monkeypatch, MODELS, 14 * GB)
    assert st["available"] is True
    assert st["used_bytes"] == 14 * GB
    assert st["ollama_bytes"] == 5 * GB
    assert st["external_bytes"] == 9 * GB
    assert st["models"][-1] == {"name": "Ollama 以外のプロセス", "size_vram": 9 * GB}


def test_fallback_to_ollama_only_without_driver(monkeypatch):
    st = _run(monkeypatch, MODELS, None)
    assert st["available"] is True
    assert st["used_bytes"] == 5 * GB
    assert st["external_bytes"] == 0
    assert [m["name"] for m in st["models"]] == ["qwen:latest"]


def test_ollama_down_but_driver_ok(monkeypatch):
    st = _run(monkeypatch, None, 3 * GB)
    assert st["available"] is True
    assert st["used_bytes"] == 3 * GB
    assert st["models"][-1]["name"] == "Ollama 以外のプロセス"


def test_both_down(monkeypatch):
    st = _run(monkeypatch, None, None)
    assert st["available"] is False
    assert st["used_bytes"] == 0
    assert st["models"] == []


def test_small_external_is_not_listed(monkeypatch):
    st = _run(monkeypatch, MODELS, 5 * GB + 10 * 1024 ** 2)
    assert st["used_bytes"] == 5 * GB + 10 * 1024 ** 2
    assert [m["name"] for m in st["models"]] == ["qwen:latest"]


def test_read_driver_vram_used_non_windows():
    if llm.os.name != "nt":
        assert llm._read_driver_vram_used() is None


def test_parse_typeperf_output(monkeypatch):
    csv_text = (
        '"(PDH-CSV 4.0)","\\\\MACHINE\\GPU Adapter Memory(luid_a)\\Dedicated Usage",'
        '"\\\\MACHINE\\GPU Adapter Memory(luid_b)\\Dedicated Usage"\r\n'
        '"10/28/2026 12:18:19.269","16719908864.000000","0.000000"\r\n'
    )

    class FakeProc:
        stdout = csv_text.encode("utf-8")

    monkeypatch.setattr(llm.os, "name", "nt")
    monkeypatch.setattr(llm.subprocess, "run", lambda *a, **k: FakeProc())
    assert llm._read_driver_vram_used() == 16719908864

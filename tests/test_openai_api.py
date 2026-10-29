"""OpenAI互換API (2026-10-29): messages変換・回答抽出・認証・設定の単体テスト。

HTTP層の統合は start_run / manager を伴うため、ここでは純粋関数のみ検証する
(実サーバでの疎通は実機デプロイ時に curl で確認済み)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import openai_api


def test_single_user_message_passes_through():
    msgs = [{"role": "user", "content": "  分数の割り算を説明して  "}]
    assert openai_api.messages_to_task(msgs) == "分数の割り算を説明して"


def test_history_is_folded_with_last_user_as_task():
    msgs = [
        {"role": "system", "content": "短く答えて"},
        {"role": "user", "content": "フランスの首都は?"},
        {"role": "assistant", "content": "パリです"},
        {"role": "user", "content": "イタリアは?"},
    ]
    task = openai_api.messages_to_task(msgs)
    assert "短く答えて" in task
    assert "User: フランスの首都は?" in task
    assert "Assistant: パリです" in task
    assert task.rstrip().endswith("イタリアは?")


def test_multipart_content_joined():
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "前半"},
        {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
        {"type": "text", "text": "後半"},
    ]}]
    assert openai_api.messages_to_task(msgs) == "前半\n後半"


def test_empty_messages():
    assert openai_api.messages_to_task([]) == ""
    assert openai_api.messages_to_task([{"role": "user", "content": "  "}]) == ""


def test_final_answer_prefers_answer_node():
    snap = {"nodes": [
        {"kind": "aggregator", "output": "統合の下書き", "tokens": 10},
        {"kind": "answer", "output": "最終回答です", "tokens": 5},
        {"kind": "task", "output": "", "tokens": 0},
    ]}
    assert openai_api.final_answer(snap) == "最終回答です"
    assert openai_api.total_tokens(snap) == 15


def test_final_answer_fallbacks():
    assert openai_api.final_answer({"nodes": [
        {"kind": "aggregator", "output": "統合のみ", "tokens": 1}]}) == "統合のみ"
    assert openai_api.final_answer({"nodes": []}) == ""


def test_auth_only_when_key_configured(monkeypatch):
    monkeypatch.setenv("AAL_API_KEY", "")
    monkeypatch.setattr(openai_api, "_settings", lambda: {})
    # キー未設定なら素通り (例外を出さない)
    openai_api._check_auth(_FakeRequest({}))
    monkeypatch.setenv("AAL_API_KEY", "secret123")
    openai_api._check_auth(_FakeRequest({"authorization": "Bearer secret123"}))
    try:
        openai_api._check_auth(_FakeRequest({"authorization": "Bearer wrong"}))
    except Exception as e:
        assert getattr(e, "status_code", None) == 401
    else:
        raise AssertionError("wrong key must be rejected")


def test_bind_host_default_and_env(monkeypatch):
    monkeypatch.delenv("AAL_HOST", raising=False)
    monkeypatch.setattr(openai_api, "_settings", lambda: {})
    assert openai_api.bind_host() == "127.0.0.1"
    monkeypatch.setenv("AAL_HOST", "0.0.0.0")
    assert openai_api.bind_host() == "0.0.0.0"


class _FakeRequest:
    def __init__(self, headers):
        self.headers = headers

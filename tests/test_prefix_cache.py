"""第3弾(2026-10-28): system プロンプト共通接頭辞の検証。

接頭辞キャッシュは会話先頭からの完全一致でしか効かないため、orchestra と
direct の全役割 system が同一の接頭辞で始まることを保証する。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import orchestrator

SYSTEMS = {
    "planner": orchestrator.PLANNER_SYSTEM,
    "subagent": orchestrator.SUBAGENT_SYSTEM_BASE,
    "aggregator": orchestrator.AGGREGATOR_SYSTEM,
    "direct": orchestrator.DIRECT_SYSTEM,
}


def test_all_role_systems_share_identical_prefix():
    prefix = orchestrator.COMMON_SYSTEM_PREFIX
    assert len(prefix) >= 30  # 再利用の意味がある長さがあること
    for name, system in SYSTEMS.items():
        assert system.startswith(prefix), f"{name} の system が共通接頭辞で始まっていない"


def test_role_bodies_survive_after_prefix():
    assert "分解するプランナー" in orchestrator.PLANNER_SYSTEM
    assert "JSON形式だけを出力" in orchestrator.PLANNER_SYSTEM
    assert "専門サブエージェント" in orchestrator.SUBAGENT_SYSTEM_BASE
    assert "統合エージェント" in orchestrator.AGGREGATOR_SYSTEM
    assert "有能なアシスタント" in orchestrator.DIRECT_SYSTEM

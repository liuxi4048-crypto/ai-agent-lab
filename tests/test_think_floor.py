"""think 連動の生成上限底上げ (2026-11-01) の検証。

根拠: think-large-design-20261031/report-cap-experiment.md
(上限 8,192 + think ありで設計タスク完結 4/4、総トークン +11%、暴走なし。
対策は think を切ることではなく、think 有効時に限り生成上限を連動して
上げること、という結論に基づく)。
"""
from orchestrator import (
    _AGGREGATE_NUM_PREDICT, _THINK_NUM_PREDICT_FLOOR,
    _apply_think_floor, _think_enabled)


def test_think_enabled_values():
    assert _think_enabled(True) is True
    assert _think_enabled("low") is True
    assert _think_enabled("medium") is True
    assert _think_enabled("high") is True
    assert _think_enabled(100) is True
    assert _think_enabled(False) is False
    assert _think_enabled("none") is False
    assert _think_enabled(None) is False
    assert _think_enabled(0) is False


def test_floor_raises_aggregate_cap_when_think_on():
    # think 有効 + 役割別上限 4096 → 下限 8192 へ底上げ (文脈には余裕あり)
    result, floored, clamped = _apply_think_floor(
        _AGGREGATE_NUM_PREDICT, True, 16384, 2000)
    assert _THINK_NUM_PREDICT_FLOOR == 8192
    assert result == 8192
    assert floored and not clamped


def test_floor_raises_with_effort_string():
    result, floored, clamped = _apply_think_floor(4096, "low", 16384, 2000)
    assert result == 8192 and floored and not clamped


def test_floor_not_applied_when_think_off():
    # think 無効なら上限は現行維持
    result, floored, clamped = _apply_think_floor(4096, False, 16384, 2000)
    assert result == 4096 and not floored and not clamped


def test_floor_not_applied_when_think_unknown():
    # モデル既定 (None) は判断材料が無いので対象外
    result, floored, clamped = _apply_think_floor(4096, None, 16384, 2000)
    assert result == 4096 and not floored and not clamped


def test_explicit_large_cap_unchanged():
    # 下限以上の明示指定 (Run の max_tokens=16384) は尊重して変えない
    result, floored, clamped = _apply_think_floor(16384, True, 16384, 2000)
    assert result == 16384 and not floored


def test_explicit_smaller_cap_raised_to_floor():
    # レポート提案: 明示指定が下限より小さい場合のみ底上げ (底上げ優先)
    result, floored, clamped = _apply_think_floor(2048, "low", 16384, 2000)
    assert result == 8192 and floored and not clamped


def test_context_clamp():
    # num_ctx 8192・プロンプト 4400字 ≒ 2200 tok → 残り 5992 までに抑える
    result, floored, clamped = _apply_think_floor(4096, True, 8192, 4400)
    assert result == 8192 - 2200
    assert floored and clamped


def test_clamp_never_lowers_below_base():
    # プロンプトが文脈の大半を占める場合は元の上限を維持する
    result, floored, clamped = _apply_think_floor(4096, True, 8192, 14000)
    assert result == 4096 and not floored and clamped


def test_small_role_cap_untouched_when_think_off():
    # 下書き・骨格などの小さい上限の役割は think なしで呼ばれる前提。
    # think 無効なら当然そのまま (適用は呼び出し側の think_floor 指定でも
    # 絞っているが、関数の意味論としても確認しておく)
    result, floored, clamped = _apply_think_floor(384, False, 16384, 1000)
    assert result == 384 and not floored and not clamped

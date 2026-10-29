"""明示モデル固定 (pinned): OpenAI / Anthropic のモデル選択と同じ単独実行。

モデルを名指ししたら、そのモデル自身が生成する。他モデルの下書き
(カスケード)・分解役の差し替え・レビュアーの自動選定・回答キャッシュを
挟まない。auto 選択時は従来の最適化層がそのまま働くことの対照も含む。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import router
import runs

CFG = {
    "default": "coder",
    "cascade_fast_model": "distill4b",
    "orchestra_planner_model": "distill4b",
    "critique_pairs": {"claude-opus": "reasoner"},
    "models": {
        "claude-opus": {"family": "claude", "strengths": ["reasoning"]},
        "deepseek_strata": {"family": "deepseek", "strengths": ["reasoning"]},
        "distill4b": {"family": "qwen", "strengths": []},
        "reasoner": {"family": "deepseek-r1", "strengths": ["reasoning"]},
        "coder": {"family": "qwen", "strengths": ["coding"]},
    },
}


def test_is_pinned_model():
    assert router.is_pinned_model("claude-opus") is True
    assert router.is_pinned_model("deepseek_strata") is True
    assert router.is_pinned_model("auto") is False
    assert router.is_pinned_model("") is False
    assert router.is_pinned_model(None) is False


def test_pinned_mode_auto_becomes_direct():
    assert router.pinned_mode("auto") == "direct"
    # 進め方まで明示された場合は尊重する
    assert router.pinned_mode("orchestra") == "orchestra"
    assert router.pinned_mode("direct") == "direct"
    assert router.pinned_mode("code") == "code"


def test_maybe_cascade_not_upgraded_when_pinned():
    # 明示した Opus / DeepSeek は高速モデルの下書きに化けない
    assert router.maybe_cascade("direct", "claude-opus", CFG, pinned=True) == "direct"
    assert router.maybe_cascade("direct", "deepseek_strata", CFG, pinned=True) == "direct"


def test_maybe_cascade_upgraded_when_auto():
    # auto 選択時は従来どおり格上げされる (対照)
    assert router.maybe_cascade("direct", "deepseek_strata", CFG, pinned=False) == "cascade"
    # 実行モデルが高速モデル自身なら格上げしない
    assert router.maybe_cascade("direct", "distill4b", CFG, pinned=False) == "direct"
    # direct 以外は触らない
    assert router.maybe_cascade("orchestra", "deepseek_strata", CFG, pinned=False) == "orchestra"


def test_orchestra_planner_not_substituted_when_pinned():
    assert router.orchestra_planner(CFG, pinned=True) is None  # 実行モデル自身が分解
    assert router.orchestra_planner(CFG, pinned=False) == "distill4b"
    assert router.orchestra_planner({"models": {}}, pinned=False) is None


def test_pick_reviewer():
    # レビュアーの明示指定が最優先
    assert router.pick_reviewer(CFG, "claude-opus", "reasoner", pinned=True) == "reasoner"
    # モデル明示時は自己批評 (critique_pairs の異モデルへ行かない)
    assert router.pick_reviewer(CFG, "claude-opus", "", pinned=True) == "claude-opus"
    assert router.pick_reviewer(CFG, "claude-opus", None, pinned=True) == "claude-opus"
    # auto 時は従来の critique_pair
    assert router.pick_reviewer(CFG, "claude-opus", "", pinned=False) == "reasoner"


def test_run_stores_pinned_flag():
    run = runs.Run("タスク", "direct", "claude-opus", pinned=True)
    assert run.pinned is True
    run2 = runs.Run("タスク", "direct", "coder")
    assert run2.pinned is False

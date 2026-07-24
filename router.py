"""model="auto" のルーティングと、批評ループの異ファミリーペア選定。

LLM分類は使わず軽量ヒューリスティック(タスク文のキーワード)で決める。
強み(models.yaml の strengths)とモードの制約(tools必須か)を尊重する。
"""
from __future__ import annotations

import re

import llm

# コーディング・実装系
_CODE_RE = re.compile(
    r"コード|実装|アプリ|スクリプト|プログラム|バグ|テスト|リファクタ|CLI|GUI|API|"
    r"作って|開発|python|javascript|typescript|html|css|sql|\.py\b|\.js\b",
    re.IGNORECASE)
# 深い推論・数学系
_REASON_RE = re.compile(
    r"数学|数式|証明|定理|確率|統計|最適化|論理|推論|考察|アルゴリズム|計算量|"
    r"なぜ|分析|評価|比較検討|戦略", re.IGNORECASE)
# 「重い」指示(明示的に最大級を求めるとき)
_HEAVY_RE = re.compile(r"難問|最高品質|じっくり|時間をかけて|徹底的に|大規模", re.IGNORECASE)


def _tools_required(mode: str) -> bool:
    return mode in ("code", "swarm-code")


def pick_model(cfg, task: str, mode: str, installed: set | None = None) -> str:
    """タスク文とモードから models.yaml のキーを選ぶ。

    installed が渡された場合、Ollamaに実在するタグのモデルだけを候補にする
    (未DLモデルへの自動ルーティングを防ぐ)。
    """
    models = cfg.get("models", {})

    def _found(tag: str) -> bool:
        return installed is None or tag in installed or f"{tag}:latest" in installed

    def ok(key: str) -> bool:
        m = models.get(key)
        return (m is not None and _found(m.get("tag", ""))
                and (not _tools_required(mode) or m.get("tools", False)))

    heavy = bool(_HEAVY_RE.search(task))
    code = bool(_CODE_RE.search(task))
    reason = bool(_REASON_RE.search(task))

    if mode == "swarm-code":
        return "worker" if ok("worker") else cfg.get("default", "coder")

    if mode == "code":
        if heavy:
            for k in ("next", "smart", "coder"):
                if ok(k):
                    return k
        for k in ("coder", "smart", "worker"):
            if ok(k):
                return k
        return cfg.get("default", "coder")

    # orchestra / critique(chat系: tools不問)
    if heavy:
        for k in ("heavy", "next", "smart"):
            if ok(k):
                return k
    if code:
        for k in ("coder", "smart", "worker"):
            if ok(k):
                return k
    if reason:
        for k in ("reasoner", "smart"):
            if ok(k):
                return k
    return "worker" if ok("worker") else cfg.get("default", "coder")


def critique_pair(cfg, author_key: str) -> str:
    """作成者キー → レビュアーキー。既定は models.yaml の critique_pairs。

    未定義なら「作成者と異なる family で reasoning を持つモデル」を探す。
    それも無ければ作成者自身(自己批評)。
    """
    pairs = cfg.get("critique_pairs", {})
    if author_key in pairs and pairs[author_key] in cfg.get("models", {}):
        return pairs[author_key]

    models = cfg.get("models", {})
    author_family = models.get(author_key, {}).get("family")
    for key, m in models.items():
        if key == author_key or m.get("family") == author_family:
            continue
        if "reasoning" in m.get("strengths", []):
            return key
    return author_key

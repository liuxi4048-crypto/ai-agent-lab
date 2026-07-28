"""タスク文から「進め方(mode)・成果物(deliverable)・モデル」を決める振り分け役。

ユーザーに枠を選ばせず、メインエージェントが決める設計:
- triage(): 高速モデルに1回だけ問い合わせて mode / deliverable を判断させる。
  失敗時は軽量ヒューリスティック(pick_mode / pick_deliverable)へフォールバックする。
- pick_model(): 強み(models.yaml の strengths)とモード制約(tools必須か)から選ぶ。
"""
from __future__ import annotations

import json
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


# 成果物形式の自動判定。「そのまま遊べる/使える」ものになりやすい形式を選ぶ。
# 判定順は exe → script → html。明示的な指定(exe/CLI等)を汎用語より優先する。
# 日本語は文字境界が効かないため、英単語は前後のASCII文字で挟まれていないかで判定する。
_EXE_RE = re.compile(
    r"\.exe|(?<![a-zA-Z])exe(?![a-zA-Z])|実行ファイル|インストーラ|"
    r"デスクトップアプリ|スタンドアロン", re.IGNORECASE)
_SCRIPT_RE = re.compile(
    r"(?<![a-zA-Z])(?:CLI|API)(?![a-zA-Z])|コマンドライン|スクリプト|バッチ処理|"
    r"ライブラリ|モジュール|サーバ|パーサ|関数を", re.IGNORECASE)
_HTML_RE = re.compile(
    r"ゲーム|(?<![a-zA-Z])game(?![a-zA-Z])|アプリ|画面|(?<![a-zA-Z])(?:UI|GUI)(?![a-zA-Z])|"
    r"ブラウザ|(?<![a-zA-Z])web(?![a-zA-Z])|ウェブ|サイト|ページ|"
    r"可視化|グラフ|チャート|ダッシュボード|アニメーション|お絵かき|時計|電卓|"
    r"パズル|クイズ|シミュレー", re.IGNORECASE)


def pick_deliverable(task: str) -> str:
    """タスク文から成果物形式(html / exe / script)を推定する。

    ゲーム・UI系は html(クリックで即動く)に寄せる。ソース一式だけ渡されるより
    実際に動かせる方が実用的なため。判断がつかないものは script(+run.bat)。
    """
    if _EXE_RE.search(task):
        return "exe"
    if _SCRIPT_RE.search(task):
        return "script"
    if _HTML_RE.search(task):
        return "html"
    return "script"


# 進め方(mode)の自動判定用。
_BUILD_RE = re.compile(
    r"作って|作成|実装|開発|直して|修正|追加|書いて|生成|セットアップ|構築|"
    r"ゲーム|アプリ|ツール|スクリプト|プログラム|コード|バグ|テスト", re.IGNORECASE)
_BIG_RE = re.compile(
    r"一式|フルスタック|複数の|いくつかの|それぞれ|同時に|まとめて|"
    r"バックエンドとフロント|API.*と.*UI|大規模", re.IGNORECASE)
_THINK_RE = re.compile(
    r"考察|検討|比較|分析|評価|レビューして|意見|提案して|アイデア|"
    r"どう思う|まとめて|要約|調査|説明して|教えて", re.IGNORECASE)
_POLISH_RE = re.compile(
    r"推敲|添削|ブラッシュアップ|磨いて|練り直|文章を直して|校正", re.IGNORECASE)


def pick_mode(task: str) -> str:
    """タスク文から進め方を推定する(LLM判定のフォールバック)。"""
    if _POLISH_RE.search(task):
        return "critique"
    build = bool(_BUILD_RE.search(task))
    if build and _BIG_RE.search(task):
        return "swarm-code"
    if build:
        return "code"
    if _THINK_RE.search(task):
        return "orchestra"
    return "code"


TRIAGE_SYSTEM = """あなたは依頼を仕分ける司令塔です。ユーザーの依頼を読み、進め方と成果物の形式を決めてください。

mode(進め方):
- "code": 何かを作る/直す。ほとんどの依頼はこれ。
- "swarm-code": 独立した部品が3つ程度に明確に分かれる大きめの制作物。迷ったら code にする。
- "orchestra": 物を作らず、調査・比較・考察・要約などの文章で answer する依頼。
- "critique": 文章を練り上げる依頼(企画書・記事などの推敲)。

deliverable(成果物の形式。mode が code/swarm-code のときだけ意味を持つ):
- "html": ゲーム・UI・可視化など、ブラウザで開けば動くもの。作る依頼では最優先で検討する。
- "exe": Windowsの実行ファイルが明示的に求められている場合。
- "script": CLIツール・ライブラリ・自動化スクリプトなど、コマンドで動かすもの。

必ず次のJSON形式だけを出力してください(説明文は不要):
{"mode": "...", "deliverable": "...", "reason": "20字以内の理由"}"""


async def triage(cfg, task: str, model_key: str) -> dict:
    """メインエージェントに mode と deliverable を決めさせる。

    1回の短いLLM呼び出し。失敗・不正出力ならヒューリスティックへフォールバックする。
    返り値: {"mode","deliverable","reason","by"} (by = "agent" | "heuristic")
    """
    fallback = {"mode": pick_mode(task), "deliverable": pick_deliverable(task),
                "reason": "キーワードから判定", "by": "heuristic"}
    try:
        msg = await llm.chat(cfg, model_key, [
            {"role": "system", "content": TRIAGE_SYSTEM},
            {"role": "user", "content": task},
        ], json_mode=True, temperature=0.0)
        raw = msg.get("content") or ""
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return fallback
        data = json.loads(m.group(0))
        mode = data.get("mode")
        deliverable = data.get("deliverable")
        if mode not in ("code", "swarm-code", "orchestra", "critique"):
            return fallback
        if mode in ("code", "swarm-code"):
            if deliverable not in ("html", "exe", "script"):
                deliverable = pick_deliverable(task)
        else:
            deliverable = None
        return {"mode": mode, "deliverable": deliverable,
                "reason": str(data.get("reason") or "")[:40], "by": "agent"}
    except Exception:
        return fallback


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

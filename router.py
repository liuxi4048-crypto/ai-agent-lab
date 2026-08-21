"""タスク文から「進め方(mode)・成果物(deliverable)・モデル」を決める振り分け役。

ユーザーに枠を選ばせず、メインエージェントが決める設計:
- triage(): 高速モデルに1回だけ問い合わせて mode / deliverable を判断させる。
  Ollama の structured outputs(JSONスキーマ)で構造を強制し、few-shot で境界事例の
  精度を上げる。失敗時は軽量ヒューリスティック(pick_mode / pick_deliverable)へ
  フォールバックする。
- pick_model(): 強み(models.yaml の strengths)とモード制約(tools必須か)から選ぶ。
"""
from __future__ import annotations

import json
import re

import llm

# 「軽い」hybrid はこの RAM オフロード量(GB)まで既定で許可する。
# coder(qwen3:30b, ram_gb=6)は 50 tok/s 実測で実用速度のため、
# 「RAM併用OFF だと主力コーダーが自動選択に乗らない」という経路矛盾を解消する。
LIGHT_HYBRID_GB = 8

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


def usable(cfg, key: str, mode: str, installed: set | None = None,
           allow_ram: bool = False, free_ram_gb: float | None = None) -> bool:
    """models.yaml のキーが今この環境で選択可能か。

    pick_model / escalation_ladder が共有する候補フィルタ。
    - installed が渡されていれば、Ollamaに実在するタグだけを可とする
    - tools 必須モードでは tools 非対応モデルを除外する
    - hybrid は ram_gb が LIGHT_HYBRID_GB 以下なら常に可(実測で実用速度)。
      それ以上は allow_ram=True のときだけ可。いずれも空きRAMが足りなければ
      不可(ページングによる激遅化を回避)。
    """
    m = cfg.get("models", {}).get(key)
    if m is None:
        return False
    # tier ゲート(2026-08-22 再設計): archive/external は自動ルーティングから常に除外。
    # probation はツール必須経路(code/swarm-code)のみ除外し、chat系の候補には残す
    # (chat難問分岐で唯一 usable な pro を無条件除外すると「最高品質」依頼が黙って
    #  worker 固定になるため)。明示指定(req.model)は pick_model を通らないので、
    # archive の heavy も model=heavy と書けば使える。
    tier = m.get("tier", "agent")
    if tier in ("archive", "external"):
        return False
    if tier == "probation" and _tools_required(mode):
        return False
    tag = m.get("tag", "")
    if not (installed is None or tag in installed or f"{tag}:latest" in installed):
        return False
    if _tools_required(mode) and not m.get("tools", False):
        return False
    if m.get("placement") == "hybrid":
        need = m.get("ram_gb", 0)
        if not allow_ram and need > LIGHT_HYBRID_GB:
            return False
        if free_ram_gb is not None and need and need > free_ram_gb:
            return False
    return True


def pick_model(cfg, task: str, mode: str, installed: set | None = None,
               allow_ram: bool = False, free_ram_gb: float | None = None) -> str:
    """タスク文とモードから models.yaml のキーを選ぶ。

    installed が渡された場合、Ollamaに実在するタグのモデルだけを候補にする
    (未DLモデルへの自動ルーティングを防ぐ)。
    hybrid の扱い: ram_gb が LIGHT_HYBRID_GB 以下の軽量 hybrid は常に候補
    (実測で実用速度)。それ以上は allow_ram=True のときだけ候補にし、
    どちらも空きRAMが足りなければ除外する(ページングによる激遅化を回避)。
    """
    models = cfg.get("models", {})

    def ok(key: str) -> bool:
        return usable(cfg, key, mode, installed, allow_ram, free_ram_gb)

    def first_ok(*keys: str) -> str | None:
        for k in keys:
            if ok(k):
                return k
        return None

    def fallback() -> str:
        d = cfg.get("default", "coder")
        return first_ok(d) or first_ok(*models.keys()) or d

    heavy = bool(_HEAVY_RE.search(task))
    code = bool(_CODE_RE.search(task))
    reason = bool(_REASON_RE.search(task))

    if mode == "swarm-code":
        return first_ok("worker", "coder", "glimmer") or fallback()

    if mode == "code":
        if heavy:
            # 並びは2026-08-20の実測(同一課題・エージェントで完走できたか)に合わせる:
            #   smart 16分◯ / glimmer 7.8分◯ / coder 14分◯ / pro 44.5分×(25反復使い切り) /
            #   next 45分で1反復のみ(単体ベンチ21tok/sでも長文脈では実効1tok/s)
            # ベンチ値ではなくエージェントで完走した実績を優先する。1課題の結果なので
            # 反例が出たら見直すこと。
            k = first_ok("smart", "glimmer", "coder")
            if k:
                return k
        return first_ok("coder", "smart", "glimmer", "worker") or fallback()

    # orchestra / critique(chat系: tools不問)
    if heavy:
        # next はエージェントループでは実効1tok/s(実測)なので後ろへ回す
        # heavy は tier=archive(明示指定のみ)なので難問の最終段は smart。
        # 既定 allow_ram=False では smart(ram_gb=10>8) が落ち pro が受ける
        k = first_ok("smart", "pro")
        if k:
            return k
    if code:
        k = first_ok("coder", "smart", "pro", "glimmer", "worker")
        if k:
            return k
    if reason:
        k = first_ok("reasoner", "pro", "smart")
        if k:
            return k
    return first_ok("worker") or fallback()


# 一次受けの候補(安価・高速な側)。VRAM全載り・tools対応のものだけを置く。
FIRST_RESPONDER_KEYS = ("worker",)

# エスカレーション先の品質順(良い順)。2026-08-20のエージェント完走実測に合わせた
# pick_model の並びと同じ根拠を使う。
_ESCALATE_CODE = ("smart", "glimmer", "coder")
_ESCALATE_CHAT = ("smart", "pro")


def escalation_ladder(cfg, task: str, mode: str, target_key: str,
                      installed: set | None = None, allow_ram: bool = False,
                      free_ram_gb: float | None = None) -> list[str]:
    """「安価な一次受け → 本命 → 上位」の順にモデルキーを並べて返す。

    pick_model は一発ルータで、最初から本命(多くは hybrid)を選ぶ。そのため
    簡単なタスクでも hybrid のロード(実測25〜28秒)とRAMスピルを毎回払う。
    2026-08-21実測(同一プロンプト / num_ctx 4096 / think off / RX 9070 XT):
      gpt-oss:20b(VRAM全載り) 108.7 tok/s ・ロード17.3s
      qwen3:30b(hybrid)        44.2 tok/s ・ロード27.1s
      nemotron-3.5-lightning   42.4 tok/s / qwen3.8:27b 21.5 / muse-glimmer 9.7
    一次受けは本命の約2.5倍速くスワップも起きないので、外れても損失が小さい。

    根拠(外部): Together AI の DeepSWE 実測では、安価なモデルを一次実行に置き
    失敗時のみ上位へ回すカスケードが、理想的な一発ルータの上限(80.8%)を上回る
    83.0% を約1/2.5のコストで達成した。単体モデルの順位ではなく配分方式が効く。

    `cascade: false` を models.yaml に置けば無効化でき、[target_key] だけを返す。
    """
    if not cfg.get("cascade", True):
        return [target_key]

    models = cfg.get("models", {})

    def ok(key: str) -> bool:
        return usable(cfg, key, mode, installed, allow_ram, free_ram_gb)

    ladder: list[str] = []

    # (1) 一次受け。本命が hybrid のときだけ前に置く。本命が既に VRAM 全載りなら
    #     カスケードの利得(ロード+スピルの回避)が無いので何も足さない。
    if models.get(target_key, {}).get("placement") == "hybrid":
        for k in FIRST_RESPONDER_KEYS:
            if k != target_key and models.get(k, {}).get("placement") == "vram" and ok(k):
                ladder.append(k)
                break

    # (2) 本命
    ladder.append(target_key)

    # (3) 上位は1段だけ。ロード時間が段ごとに二重に乗るので深追いしない。
    order = _ESCALATE_CODE if mode in ("code", "swarm-code") else _ESCALATE_CHAT
    if target_key in order:
        for k in order[:order.index(target_key)]:
            if k not in ladder and ok(k):
                ladder.append(k)
                break

    return ladder


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
# _BUILD_VERB_RE = 明確な制作動詞。名詞だけ(「このコードは〜」等)では build 扱いしない。
_BUILD_VERBS = r"作って|作成|実装|開発|直して|修正|追加|書いて|生成|セットアップ|構築"
_BUILD_VERB_RE = re.compile(_BUILD_VERBS, re.IGNORECASE)
_BUILD_RE = re.compile(
    _BUILD_VERBS + r"|ゲーム|アプリ|ツール|スクリプト|プログラム|コード|バグ|テスト",
    re.IGNORECASE)
_BIG_RE = re.compile(
    r"一式|フルスタック|複数の|いくつかの|それぞれ|同時に|まとめて|"
    r"バックエンドとフロント|API.*と.*UI|大規模", re.IGNORECASE)
_THINK_RE = re.compile(
    r"考察|検討|比較|分析|評価|レビューして|意見|提案して|アイデア|"
    r"どう思う|まとめて|要約|調査|説明して|教えて|なぜ", re.IGNORECASE)
_POLISH_RE = re.compile(
    r"推敲|添削|ブラッシュアップ|磨いて|練り直|文章を直して|校正", re.IGNORECASE)


def pick_mode(task: str) -> str:
    """タスク文から進め方を推定する(LLM判定のフォールバック)。"""
    if _POLISH_RE.search(task):
        return "critique"
    # 「このコードがなぜ動かないか説明して」のような質問は、コード系名詞を含んでいても
    # 調査・考察(orchestra)。制作動詞が無い質問文を build に誤判定しない。
    if _THINK_RE.search(task) and not _BUILD_VERB_RE.search(task):
        return "orchestra"
    build = bool(_BUILD_RE.search(task))
    if build and _BIG_RE.search(task):
        return "swarm-code"
    if build:
        return "code"
    if _THINK_RE.search(task):
        return "orchestra"
    return "code"


# structured outputs 用スキーマ。浅く小さく保つ(深いネスト・巨大enumは制約デコードを遅くする)。
# 注意: スキーマの description は文法制約にしか使われずモデルには届かない。
# フィールドの意味は必ずプロンプト本文に書く。
TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["code", "swarm-code", "orchestra", "critique"]},
        "deliverable": {"type": "string", "enum": ["html", "exe", "script", "none"]},
        "reason": {"type": "string"},
    },
    "required": ["mode", "deliverable", "reason"],
}

TRIAGE_SYSTEM = """あなたは依頼を仕分ける司令塔です。ユーザーの依頼を読み、進め方と成果物の形式を決めてください。

mode(進め方):
- "code": 何かを作る/直す。ほとんどの依頼はこれ。
- "swarm-code": 独立した部品が3つ程度に明確に分かれる大きめの制作物。迷ったら code にする。
- "orchestra": 物を作らず、調査・比較・考察・要約・説明などの文章で答える依頼。
- "critique": 文章を練り上げる依頼(企画書・記事などの推敲)。

deliverable(成果物の形式):
- "html": ゲーム・UI・可視化など、ブラウザで開けば動くもの。作る依頼では最優先で検討する。
- "exe": Windowsの実行ファイルが明示的に求められている場合。
- "script": CLIツール・ライブラリ・自動化スクリプトなど、コマンドで動かすもの。
- "none": mode が orchestra / critique のとき(何も作らない)。

判定例:
- 「ブロック崩しゲームを作って」→ {"mode":"code","deliverable":"html","reason":"ブラウザゲーム制作"}
- 「CSVを集計するCLIツールを書いて」→ {"mode":"code","deliverable":"script","reason":"CLIツール制作"}
- 「このコードがなぜ動かないのか説明して」→ {"mode":"orchestra","deliverable":"none","reason":"説明を求める質問"}
- 「フロントとAPIとDBのTODOアプリ一式を作って」→ {"mode":"swarm-code","deliverable":"script","reason":"独立部品3つの制作"}
- 「この企画書を推敲して」→ {"mode":"critique","deliverable":"none","reason":"文章の推敲"}
- 「ポモドーロタイマーのデスクトップアプリ(exe)が欲しい」→ {"mode":"code","deliverable":"exe","reason":"exe明示指定"}

必ず {"mode": "...", "deliverable": "...", "reason": "20字以内の理由"} のJSONだけを出力してください。"""


async def triage(cfg, task: str, model_key: str) -> dict:
    """メインエージェントに mode と deliverable を決めさせる。

    1回の短いLLM呼び出し(スキーマ強制・低温度・30秒/リトライ2回・出力上限付き)。
    失敗・不正出力ならヒューリスティックへフォールバックする。
    返り値: {"mode","deliverable","reason","by"} (by = "agent" | "heuristic")
    """
    fallback = {"mode": pick_mode(task), "deliverable": pick_deliverable(task),
                "reason": "キーワードから判定", "by": "heuristic"}
    try:
        # num_predict は thinking も消費する。gpt-oss系は既定 reasoning(medium)が
        # 分類前に数百トークン使うため、effort を low に落とし予算も余裕を持たせる
        # (qwen系は thinking を切る)。足りないと content が空になり毎回フォール
        # バックしてしまう。
        info = llm.resolve(cfg, model_key)
        think = llm.effort(info, "low")
        if info["family"] == "qwen":
            think = False
        msg = await llm.chat(cfg, model_key, [
            {"role": "system", "content": TRIAGE_SYSTEM},
            {"role": "user", "content": task},
        ], json_schema=TRIAGE_SCHEMA, temperature=0.1,
            num_predict=600, timeout=60, retries=2, think=think)
        raw = (msg.get("content") or "").strip()
        data = json.loads(raw)
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
    except Exception as e:
        # フォールバック率の観測用(サーバーログに残す)
        print(f"[triage] LLM判定失敗→ヒューリスティック: {type(e).__name__}: {e}")
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

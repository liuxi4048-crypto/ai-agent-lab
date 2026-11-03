"""簡体字中国語の混入検出と、最終回答の再生成ガード。

DeepSeek (IQ2_XS 量子化) の回答に簡体字中国語が混ざる既知の癖への対策。
混入の程度は実行ごとに大きくブレる (2026-10-28 実測: 良い Run は score 1、
悪い Run は score 53)。検出は決定的な文字・語句ベースで行い、閾値を超えたら
「日本語のみ」を強く指示して 1 回だけ再生成し、混入の少ない方を採用する。

閾値の根拠 (2026-10-28、実 Run の最終回答 5 件で較正):
  - きれいな日本語 (Run 493f6dcb84): score 1
  - 軽微 (也就是说 が 1 箇所だけ, Run 76f103cfcb): score 5
  - 中国語の文が複数埋め込まれた (Run 641f4cfc18): score 19
  - 顕著な混入 (Run d7ed2ecac3): score 35
  - 大半が中国語 (Run 10c0a96248): score 53
閾値 12 は「軽微」と「文レベルの混入」の間にある。
"""

# 簡体字専用字: 現代日本語の通常文では使われない字形。
# 日本語でも使う字 (的・是・点・来・被・画・国・学・当・将・万) は含めない
# (誤検出で再生成コストを払わないため)。
STRONG_CHARS = frozenset(
    "么这们还现习问题开关发动东车马书让给应种样总每进远运达听观规该论据确项须类组结"
    "领难处备称调陆际险随集仅极构标说话语认个为时过对经已很门间长乐队办务实够"
)

# 中国語の機能語・言い回し (2 字以上)。日本語の通常文にはまず現れない。
BIGRAMS = (
    "的是", "就是", "我们", "他们", "没有", "不是", "但是", "所以",
    "因为", "如果", "的话", "什么", "怎么", "这样", "那个", "这个", "已经",
    "还是", "或者", "而且", "然后", "觉得", "知道", "出来", "起来", "对于",
    "关于", "通过", "进行", "需要", "可以说", "也就是说", "为什么", "里面",
    "之后", "之前", "时候", "人们", "两个", "一个", "一些", "这些", "那些",
)

CONTAMINATION_THRESHOLD = 12

# 生成途中の早期打ち切り判定: ある程度の長さが出た時点で、スコアが閾値を
# 超え、かつ密度 (1000 字あたり) も高い場合は、最後まで中国語混じりに
# なる見込みが高いとして打ち切る。混入した実サンプルは冒頭から密度が
# 高く (per1k 20 超)、きれいな日本語や軽微な混入 (per1k 5 前後) では
# 発動しない水準に置く。
EARLY_ABORT_MIN_CHARS = 400
EARLY_ABORT_MIN_PER1K = 20

# 再生成時に system へ追加する強い指示。
RETRY_CLAUSE = (
    "\n\n重要: 前回の出力には中国語 (簡体字) が混入していました。"
    "今回は日本語のみで書いてください。簡体字や中国語の言い回し "
    "(例: 也就是说・因为・然后・什么) を一切使わないでください。"
)


def contamination_detail(text: str) -> dict:
    """混入スコアの内訳。score = 簡体字専用字 + 2×機能語 + 全角コンマ。"""
    text = text or ""
    strong = sum(text.count(c) for c in STRONG_CHARS)
    bigram = sum(text.count(b) for b in frozenset(BIGRAMS))
    # 全角コンマ「，」は中国語文の句読点。日本語の回答は通常「、」を使う
    fw_comma = text.count("，")
    return {
        "strong": strong,
        "bigram": bigram,
        "fw_comma": fw_comma,
        "score": strong + 2 * bigram + fw_comma,
        "chars": len(text),
    }


def contamination_score(text: str) -> int:
    return contamination_detail(text)["score"]


def is_contaminated(text: str) -> bool:
    return contamination_score(text) >= CONTAMINATION_THRESHOLD


def partial_is_contaminated(text: str) -> bool:
    """生成途中の部分テキストが混入見込みか (早期打ち切り用)。"""
    if len(text or "") < EARLY_ABORT_MIN_CHARS:
        return False
    d = contamination_detail(text)
    return (
        d["score"] >= CONTAMINATION_THRESHOLD
        and 1000 * d["score"] / d["chars"] >= EARLY_ABORT_MIN_PER1K
    )

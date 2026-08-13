"""外部レビューパネル: ローカルLLM(Ollama)/Codex を「別思考のレビュアー」として並列に走らせるCLI。

Claude Code のサブエージェント(~/.claude/agents/local-reviewer.md, codex-reviewer.md)から
呼ばれることを想定。観点(lens)ごとに独立したコンテキストでレビューさせ、結果を束ねて返す。

VRAM 16GB(RX 9070 XT)前提のスケジューリング:
- モデル単位は**直列**に処理する。異なるモデルを同時に走らせるとVRAMに載りきらず
  ロード/アンロードのスラッシングで逆に遅くなるため(worker 13GB + reasoner 9.3GB = 22GB)。
- 同一モデルに割り当たった複数lensは**並列**に投げる。既にロード済みのモデルへの
  並列リクエストはOllamaが捌けるのでスワップが起きない。
- Codexバックエンドは別プロセス(クラウド実行)なのでVRAMを食わず、Ollama群と同時に走らせる。
- hybrid配置モデルは llm._HYBRID_GATE がプロセス内でさらに直列化する。

使用例:
    python panel.py --target app.py --lens correctness,security
    python panel.py --diff C:\\dev\\myrepo --preset deep --format json
    python panel.py --check
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time

import llm

# ---------------------------------------------------------------- lens 定義

# model: models.yaml のキー。未インストールなら FALLBACK 順に降格する。
LENSES = {
    "correctness": {
        "model": "reasoner",
        "focus": "正確性。ロジックの誤り、境界条件(空・0・最大値・負値)、off-by-one、"
                 "例外/エラー処理の欠落、null/undefined、競合状態、リソース解放漏れ。",
    },
    "security": {
        "model": "worker",
        "focus": "セキュリティ。入力検証の欠落、インジェクション(SQL/コマンド/パス)、"
                 "認証・認可の抜け、秘密情報のハードコードやログ出力、安全でないデシリアライズ、"
                 "権限昇格につながる既定値。",
    },
    "perf": {
        "model": "worker",
        "focus": "性能。不要なループのネスト、N+1、同期I/Oのブロッキング、"
                 "大きなオブジェクトの無駄なコピー、キャッシュ可能な再計算、計算量の悪化。",
    },
    "design": {
        "model": "smart",
        "focus": "設計。責務の分離、過剰な結合、抽象の漏れ、拡張時に壊れる箇所、"
                 "命名と実体の不一致、既存パターンからの逸脱。",
    },
    "simplify": {
        "model": "coder",
        "focus": "簡素化。重複コード、不要な分岐・変数・抽象、標準ライブラリで置換できる自作処理、"
                 "デッドコード。挙動を変えずに削れる部分だけを挙げる。",
    },
}

PRESETS = {
    "quick": ["correctness"],
    "default": ["correctness", "security", "perf"],
    "deep": ["correctness", "security", "perf", "design", "simplify"],
}

# 希望モデルが未インストールのときの降格順(vram配置=軽いものを優先)
FALLBACK = ["worker", "reasoner", "fast", "coder", "smart", "heavy"]

SYSTEM_PROMPT = """あなたはコードレビュアーです。与えられた観点**だけ**に集中し、他の観点は無視してください。

出力は日本語のMarkdown。指摘ごとに次の形式:

### <一行要約>
- 重大度: high | medium | low
- 場所: <ファイル:行 または 関数名>
- 根拠: <該当コードの引用 1〜3行>
- 内容: <何が問題か、どう直すか>

規則:
- 与えられたコードから実際に確認できることだけ書く。推測・一般論・お世辞は書かない。
- 確信が持てない指摘は出さない。誤検出は見逃しより有害。
- 該当する問題が無ければ「指摘なし」とだけ出力する。
- 前置き・要約・締めの文は書かない。指摘だけを出力する。
"""

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
_OPEN_THINK_RE = re.compile(r"^\s*<think>.*", re.DOTALL | re.IGNORECASE)

# codex CLI の起動コマンド。プロンプトは末尾の `-` により**標準入力**から渡す
# (Windowsのコマンドライン長上限32767を避けるため)。`--sandbox read-only` で
# レビュアーがファイルを書き換えないよう封じる。
# 環境変数 PANEL_CODEX_CMD で上書き可能(codex のCLI仕様変更に追従するため)。
DEFAULT_CODEX_CMD = "codex exec --skip-git-repo-check --sandbox read-only --color never -"


def strip_think(text):
    """DeepSeek-R1系の <think> ブロックを除去する(閉じタグ欠落時は全体を捨てる)。"""
    text = _THINK_RE.sub("", text)
    return _OPEN_THINK_RE.sub("", text).strip()


def _next_pow2(n):
    """n以上の最小の2の冪を返す。"""
    p = 1
    while p < n:
        p *= 2
    return p


# ---------------------------------------------------------------- 入力の収集

def collect_content(targets, diff_repo, max_chars):
    """レビュー対象のテキストを組み立てる。戻り値: (本文, 由来ラベル)"""
    parts, labels = [], []

    if diff_repo:
        proc = subprocess.run(["git", "-C", diff_repo, "diff", "HEAD"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
        if proc.returncode != 0:
            raise SystemExit(f"git diff に失敗: {proc.stderr.strip()[:300]}")
        if not proc.stdout.strip():
            raise SystemExit(f"{diff_repo} に未コミットの差分がありません。")
        parts.append(f"--- git diff HEAD ({diff_repo}) ---\n{proc.stdout}")
        labels.append(f"diff:{os.path.basename(os.path.abspath(diff_repo))}")

        # git diff HEAD には未追跡(git add前)の新規ファイルが乗らないため、
        # 別途 ls-files で拾って本文に加える(1ファイルあたり4000字上限)
        untracked = subprocess.run(
            ["git", "-C", diff_repo, "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        new_files = [p.strip() for p in (untracked.stdout or "").splitlines() if p.strip()]
        for rel in new_files:
            try:
                with open(os.path.join(diff_repo, rel), "r", encoding="utf-8", errors="replace") as f:
                    text = f.read(4000)
            except OSError:
                continue
            parts.append(f"--- new file: {rel} ---\n{text}")
        if new_files:
            labels.append(f"new:{len(new_files)}件")

    for t in targets:
        if t == "-":
            text = sys.stdin.read()
            parts.append(f"--- stdin ---\n{text}")
            labels.append("stdin")
            continue
        if os.path.isdir(t):
            raise SystemExit(f"ディレクトリは対象にできません(ファイルを指定): {t}")
        with open(t, "r", encoding="utf-8", errors="replace") as f:
            parts.append(f"--- {t} ---\n{f.read()}")
        labels.append(os.path.basename(t))

    if not parts:
        raise SystemExit("--target か --diff のどちらかを指定してください。")

    body = "\n\n".join(parts)
    if len(body) > max_chars:
        body = body[:max_chars] + f"\n\n...(以降 {len(body) - max_chars} 文字を省略)"
    return body, ", ".join(labels)


def build_prompt(lens_name, focus, content, source, instruction):
    extra = f"\n追加の指示: {instruction}\n" if instruction else ""
    return (
        f"観点: {lens_name}\n"
        f"この観点で見るべき点: {focus}\n"
        f"レビュー対象: {source}\n{extra}\n"
        f"以下をレビューしてください。\n\n{content}\n"
    )


# ---------------------------------------------------------------- バックエンド

async def run_ollama_lens(cfg, model_key, lens_name, content, source, instruction):
    started = time.monotonic()
    info = llm.resolve(cfg, model_key)
    num_ctx = info["num_ctx"]

    prompt = build_prompt(lens_name, LENSES[lens_name]["focus"], content, source, instruction)
    # 概算トークン数(日本語想定でlen//2)がコンテキストの80%を超える場合は
    # num_ctx を「必要量÷0.8」(=SYSTEM_PROMPT・生成分のヘッドルーム込み)まで
    # 32768上限で広げる。上限でも80%予算に収まらない分は対象コード部分の末尾を
    # 切り詰める(境界帯 26K〜32K トークンを無余裕のまま送ると、Ollamaが先頭の
    # system/観点指示から黙って切り捨てるため)。
    req_num_ctx = None
    estimated = (len(prompt) + len(SYSTEM_PROMPT)) // 2
    if estimated > num_ctx * 0.8:
        needed = _next_pow2(int(estimated / 0.8))
        req_num_ctx = min(32768, needed)
        budget_tokens = int(req_num_ctx * 0.8)
        if estimated > budget_tokens:
            overhead_tokens = estimated - (len(content) // 2)   # focus/指示文などcontent以外の分
            keep_chars = max(0, budget_tokens - overhead_tokens) * 2
            if keep_chars < len(content):   # 実際に削れる分がある場合だけ切り詰める
                content = content[:keep_chars] + "\n\n…(入力が長いため末尾を省略)"
                prompt = build_prompt(lens_name, LENSES[lens_name]["focus"], content, source, instruction)

    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}]
    try:
        msg = await llm.chat(cfg, model_key, messages, temperature=0.2, num_ctx=req_num_ctx)
        text = strip_think(msg.get("content", ""))
        if not text:
            # <think>ブロックだけで本文が無い応答。temperatureを上げて1回だけ救済を試みる
            retry = await llm.chat(cfg, model_key, messages, temperature=0.8, num_ctx=req_num_ctx)
            text = strip_think(retry.get("content", ""))
        error = None if text else "思考のみで本文なし"
        return {"lens": lens_name, "backend": "ollama", "model": info["tag"],
                "seconds": round(time.monotonic() - started, 1),
                "output": text or "(空応答)", "error": error}
    except Exception as e:  # llm.OllamaError 含む。1lensの失敗で全体を落とさない
        return {"lens": lens_name, "backend": "ollama", "model": info["tag"],
                "seconds": round(time.monotonic() - started, 1),
                "output": "", "error": f"{type(e).__name__}: {e}"}


def resolve_codex_argv(template):
    """codex の実体をPATHから解決する。戻り値: (argv, 表示名)。未検出なら (None, 名前)。

    npm製の shim は `.cmd` なので CreateProcess から直接起動できない(shutil.which では
    見つかるのに subprocess が FileNotFoundError になる)。cmd /c 経由に包む。
    """
    tokens = template.split()
    exe = shutil.which(tokens[0])
    if exe is None:
        return None, tokens[0]
    if exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe] + tokens[1:], tokens[0]
    return [exe] + tokens[1:], tokens[0]


async def run_codex_lens(lens_name, content, source, instruction, cwd):
    started = time.monotonic()
    template = os.environ.get("PANEL_CODEX_CMD", DEFAULT_CODEX_CMD)
    argv, name = resolve_codex_argv(template)

    def result(output, error):
        return {"lens": lens_name, "backend": "codex", "model": name,
                "seconds": round(time.monotonic() - started, 1),
                "output": output, "error": error}

    if argv is None:
        return result("", f"{name} が見つかりません。`npm i -g @openai/codex` の後 `codex login` が必要。")

    # プロンプトは標準入力から渡す(codex exec の末尾 `-` がstdin読み取りを指示する)
    prompt = SYSTEM_PROMPT + "\n\n" + build_prompt(
        lens_name, LENSES[lens_name]["focus"], content, source, instruction)

    def _run():
        return subprocess.run(argv, input=prompt, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", cwd=cwd, timeout=900)

    try:
        proc = await asyncio.to_thread(_run)
    except subprocess.TimeoutExpired:
        return result("", "codex がタイムアウト(900秒)")

    # codexは起動バナーをstderrに出すため、実際の失敗理由が出る**末尾**を残す
    err = None if proc.returncode == 0 else f"codex exit {proc.returncode}: ...{proc.stderr.strip()[-800:]}"
    return result(proc.stdout.strip(), err)


async def run_codex_all(lens_names, content, source, instruction, cwd):
    return list(await asyncio.gather(*[
        run_codex_lens(n, content, source, instruction, cwd) for n in lens_names
    ]))


# ---------------------------------------------------------------- スケジューリング

def resolve_models(cfg, lens_names, installed, override):
    """lens名 → 実際に使うmodels.yamlキー。未インストールは FALLBACK 順に降格。"""
    def available(key):
        m = cfg.get("models", {}).get(key)
        if not m:
            return False
        tag = m.get("tag", "")
        return tag in installed or f"{tag}:latest" in installed

    mapping = {}
    for name in lens_names:
        want = override or LENSES[name]["model"]
        if available(want):
            mapping[name] = want
            continue
        mapping[name] = next((k for k in FALLBACK if available(k)),
                             cfg.get("default", "coder"))
    return mapping


async def run_ollama_all(cfg, mapping, content, source, instruction):
    """モデル単位は直列、同一モデル内のlensは並列(VRAMスワップを避ける)。"""
    groups = {}
    for lens_name, key in mapping.items():
        groups.setdefault(key, []).append(lens_name)

    results = []
    for key, lens_names in groups.items():
        results.extend(await asyncio.gather(*[
            run_ollama_lens(cfg, key, n, content, source, instruction) for n in lens_names
        ]))
    return results


# ---------------------------------------------------------------- 出力

def render_markdown(results, source, elapsed):
    ok = [r for r in results if not r["error"]]
    ng = [r for r in results if r["error"]]
    head = (f"# 外部レビューパネル\n\n"
            f"対象: {source} / レビュアー {len(results)}体 / 合計 {elapsed:.0f}秒\n")
    body = []
    for r in sorted(results, key=lambda x: x["lens"]):
        title = f"\n## [{r['lens']}] {r['backend']}:{r['model']} ({r['seconds']}秒)\n"
        body.append(title + (f"\n**失敗**: {r['error']}\n" if r["error"] else f"\n{r['output']}\n"))
    tail = f"\n---\n成功 {len(ok)} / 失敗 {len(ng)}\n"
    return head + "".join(body) + tail


async def cmd_check(cfg):
    alive = await llm.is_alive()
    installed = set(await llm.list_models()) if alive else set()
    print(f"Ollama ({llm.OLLAMA_BASE}): {'UP' if alive else 'DOWN'}")
    if alive:
        for key in sorted(cfg.get("models", {})):
            info = llm.resolve(cfg, key)
            mark = "OK " if (info["tag"] in installed
                             or f"{info['tag']}:latest" in installed) else "未DL"
            print(f"  {mark} {key:9s} {info['tag']} ({info['placement']})")
    argv, name = resolve_codex_argv(os.environ.get("PANEL_CODEX_CMD", DEFAULT_CODEX_CMD))
    if argv is None:
        print(f"codex CLI: NOT FOUND ({name}) — npm i -g @openai/codex && codex login")
    else:
        print(f"codex CLI: {' '.join(argv[:3])}")
        auth = os.path.join(os.path.expanduser("~"), ".codex", "auth.json")
        print(f"  ログイン: {'済' if os.path.exists(auth) else '未 (codex login が必要)'}")
    print("\nlens:")
    for name, spec in LENSES.items():
        print(f"  {name:12s} -> {spec['model']}")
    print("preset: " + ", ".join(f"{k}({len(v)})" for k, v in PRESETS.items()))
    return 0 if alive else 1


# ---------------------------------------------------------------- エントリポイント

async def main_async(args):
    cfg = llm.load_config()

    if args.check:
        return await cmd_check(cfg)

    lens_names = ([n.strip() for n in args.lens.split(",") if n.strip()]
                  if args.lens else PRESETS[args.preset])
    unknown = [n for n in lens_names if n not in LENSES]
    if unknown:
        raise SystemExit(f"未知のlens: {', '.join(unknown)} / 選択肢: {', '.join(LENSES)}")

    content, source = collect_content(args.target, args.diff, args.max_chars)
    started = time.monotonic()
    tasks = []

    if args.backend in ("ollama", "both"):
        if not await llm.is_alive():
            raise SystemExit(f"Ollamaに接続できません({llm.OLLAMA_BASE})。`ollama serve` を確認。")
        installed = set(await llm.list_models())
        mapping = resolve_models(cfg, lens_names, installed, args.model)
        tasks.append(run_ollama_all(cfg, mapping, content, source, args.instruction))

    if args.backend in ("codex", "both"):
        cwd = args.diff or (os.path.dirname(os.path.abspath(args.target[0]))
                            if args.target and args.target[0] != "-" else os.getcwd())
        tasks.append(run_codex_all(lens_names, content, source, args.instruction, cwd))

    gathered = await asyncio.gather(*tasks)
    results = [r for group in gathered for r in group]
    elapsed = time.monotonic() - started

    if args.format == "json":
        print(json.dumps({"source": source, "seconds": round(elapsed, 1),
                          "results": results}, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(results, source, elapsed))
    # 全滅時だけでなく、半数以上のlensが失敗した場合も失敗として exit 1 にする
    failed = sum(1 for r in results if r["error"])
    if not results or failed * 2 >= len(results):
        return 1
    return 0


def main():
    p = argparse.ArgumentParser(
        description="ローカルLLM/Codexを並列レビュアーとして走らせる外部レビューパネル")
    p.add_argument("--target", action="append", default=[],
                   help="レビュー対象ファイル(複数可)。'-' で標準入力")
    p.add_argument("--diff", help="このgitリポジトリの `git diff HEAD` を対象にする")
    p.add_argument("--lens", help=f"観点をカンマ区切りで指定: {', '.join(LENSES)}")
    p.add_argument("--preset", default="default", choices=list(PRESETS),
                   help="lens未指定時のプリセット(既定: default)")
    p.add_argument("--backend", default="ollama", choices=["ollama", "codex", "both"])
    p.add_argument("--model", help="全lensのモデルをこのmodels.yamlキーで上書き")
    p.add_argument("--instruction", help="全レビュアーへの追加指示")
    p.add_argument("--max-chars", type=int, default=60000, help="対象本文の最大文字数")
    p.add_argument("--format", default="md", choices=["md", "json"])
    p.add_argument("--check", action="store_true", help="疎通とモデル/lens一覧を表示して終了")
    args = p.parse_args()

    try:
        sys.exit(asyncio.run(main_async(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()

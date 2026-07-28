"""ツール群 + 安全策(パス限定 / denylist / 実行前承認)。

エージェントの「手足」。生成物は WORKSPACE(= projects/)配下に限定する。
安全の本丸は「危険操作の実行前承認(既定ON)」。denylist は補助線(回避容易)。

並列実行対応(v2):
- グローバル APPROVER を廃止し、Run ごとに Toolbox インスタンスを持つ。
  承認コールバックは async callable(command, cwd) -> bool。
- subdir 指定で projects/<run_id>/... をルートにでき、並列コーダー同士の
  ファイル衝突を防ぐ(swarm-code は sub_0/ sub_1/ ... を割り当てる)。
- run_command は asyncio.create_subprocess_shell + wait_for(タイムアウト)。

出力品質向上(v3):
- edit_file: 部分置換。小型モデルの「全文書き直しで既存コードを削る」事故を防ぐ。
- search_files: grep相当。全文readでコンテキストを潰さずにコードを探せる。
- write_file/edit_file 後に構文チェック(py/json)を自動実行し、その場で気づかせる。
"""
import ast
import asyncio
import fnmatch
import json
import os
import re

BASE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.join(BASE, "projects")

# denylist は回避容易(例 python -c "shutil.rmtree(...)")。あくまで明白な事故の一次防止。
# 本当の安全は承認モード(既定ON)と、発展のコンテナ/別ユーザ隔離で担保する。
DENY_PATTERNS = [
    r"\brm\s+-rf?\b", r"\brmdir\s+/s\b", r"\bdel\s+/[sqf]", r"\brd\s+/s\b",
    r"\bformat\b", r"\bmkfs\b", r"\bdiskpart\b", r"\bdd\s+if=",
    r"git\s+reset\s+--hard", r"git\s+clean\s+-[a-z]*f", r"push\s+.*(--force|-f)\b",
    r"\bshutdown\b", r"\breboot\b", r"\btaskkill\s+/f", r"\breg\s+delete\b",
    r"shutil\.rmtree", r":\s*>\s*/", r">\s*/dev/sd",
]
DENY_RE = re.compile("|".join(DENY_PATTERNS), re.IGNORECASE)

# 承認OFF(自走)時のみ適用するワークスペース脱出検知。
# working_dirは_safe_pathで制限済みだが、コマンド文字列自体はshellに素通しのため、
# 絶対パス・UNC・環境変数展開・パス要素としての .. を無人実行では拒否する。
# (承認ON時は人間の確認が最後の砦なのでスキャンしない)
ESCAPE_PATTERNS = [
    r"\b[A-Za-z]:(?=\\|/(?!/))",                    # ドライブ絶対パス C:\ / C:/(URLの :// は除外)
    r"\\\\",                                          # UNCパス
    r"%\w+%|\$env:",                                  # 環境変数展開
    r"(?:^|[\s\"'=(\\/])\.\.(?=[\\/\s\"'&|;]|$)",  # パス要素としての ..
]
ESCAPE_RE = re.compile("|".join(ESCAPE_PATTERNS))

RESULT_CAP = 12000   # ツール結果の履歴挿入上限(コンテキスト保護の最終防衛線)
MAX_TIMEOUT = 600    # run_command でモデルが指定できるタイムアウトの上限秒
SEARCH_SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv", "dist", "build"}
# 成果物検証用。dist/ の exe を見つける必要があるのでビルド出力は除外しない
VERIFY_SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv"}
# 「実際に動くもの」とみなす拡張子(.md 等の説明ファイルだけで終わらせないための判定用)
CODE_EXT = (".py", ".js", ".mjs", ".ts", ".html", ".htm", ".java", ".c", ".cpp", ".cs",
            ".go", ".rs", ".rb", ".php", ".sh", ".bat", ".cmd", ".exe", ".ps1", ".jsx", ".tsx")


_SCRIPT_BLOCK_RE = re.compile(r"<script\b[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)
_SCRIPT_SRC_RE = re.compile(r"<script\b[^>]*\bsrc\s*=", re.IGNORECASE)
_node_available = None  # None=未判定 / True / False


async def _js_syntax_check(path, content):
    """HTML内のインラインJS・.js を Node で構文チェックする。

    Node が無い環境では黙ってスキップする(必須依存にしない)。
    HTMLアプリはエージェントが実行して確かめられないため、せめて構文崩れは
    書いた直後に気づけるようにする。
    """
    global _node_available
    if _node_available is False:
        return None
    ext = os.path.splitext(path)[1].lower()
    if ext in (".js", ".mjs"):
        js = content
    elif ext in (".html", ".htm"):
        blocks = [m.group(1) for m in _SCRIPT_BLOCK_RE.finditer(content)
                  if not _SCRIPT_SRC_RE.match(m.group(0))]
        js = "\n;\n".join(b for b in blocks if b.strip())
    else:
        return None
    if not js.strip():
        return None

    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), f"_agentlab_check_{os.getpid()}.js")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(js)
        proc = await asyncio.create_subprocess_exec(
            "node", "--check", tmp,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    except FileNotFoundError:
        _node_available = False   # Node未導入。以後スキップ
        return None
    except (OSError, asyncio.TimeoutError):
        return None
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    _node_available = True
    if proc.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip().splitlines()
        head = next((ln for ln in detail if "Error" in ln or "error" in ln), "")
        return f"⚠ JavaScriptの構文エラー: {head[:200]}。直して書き直すこと"
    return None


_ID_RE = re.compile(r"""\bid\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_EXTERNAL_RE = re.compile(r"""<(?:script|link|img)\b[^>]*\b(?:src|href)\s*=\s*["']https?://""",
                          re.IGNORECASE)
_BOOT_RE = re.compile(
    r"window\.onload|DOMContentLoaded|addEventListener\s*\(\s*['\"]load|"
    r"setInterval\s*\(|requestAnimationFrame\s*\(|"
    r"^\s*(?:init|main|start|loop|draw|render|setup|game|update)\s*\(\s*\)\s*;",
    re.MULTILINE | re.IGNORECASE)

# 最小DOMスタブ。実ブラウザではなく「読み込み時に落ちないか」だけを検査する。
# 肝は getElementById/querySelector が HTML に実在しないIDでは null を返すこと
# (存在しない要素を掴んで TypeError で即死する典型バグを捕まえる)。
_DOM_HARNESS = r"""
const KNOWN_IDS = new Set(__IDS__);
const KNOWN_TAGS = new Set(__TAGS__);
const HANDLERS = [];   // 登録されたイベントハンドラ(読み込み後に一度だけ発火させる)
const rec = (type, fn) => { if (typeof fn === 'function') HANDLERS.push([String(type), fn]); };
const mk = (name) => new Proxy(function(){}, {
  get(t, p) {
    if (p === Symbol.toPrimitive) return () => 0;
    if (p === Symbol.iterator) return function*(){};
    if (p === 'length') return 0;
    if (p === 'then') return undefined;
    if (p === 'getImageData') return () => ({ data: new Uint8ClampedArray(4) });
    if (p === 'getContext') return () => mk(name + '.ctx');
    if (p === 'toString') return () => name;
    if (p === 'addEventListener') return (t, fn) => rec(t, fn);
    return mk(name + '.' + String(p));
  },
  set() { return true; },
  apply() { return mk(name + '()'); },
  has() { return true; },
});
const el = (n) => mk(n);
const sel = (s) => {
  s = String(s || '').trim();
  if (s.startsWith('#')) return KNOWN_IDS.has(s.slice(1)) ? el(s) : null;
  const tag = s.split(/[ .:\[>]/)[0].toLowerCase();
  return (!tag || KNOWN_TAGS.has(tag)) ? el(s) : null;
};
globalThis.document = new Proxy({
  getElementById: (id) => (KNOWN_IDS.has(String(id)) ? el('#' + id) : null),
  querySelector: sel,
  querySelectorAll: (s) => (sel(s) ? [el(s)] : []),
  getElementsByTagName: (t) => (KNOWN_TAGS.has(String(t).toLowerCase()) ? [el(t)] : []),
  getElementsByClassName: () => [],
  createElement: (t) => el(t),
  createTextNode: () => el('#text'),
  addEventListener: (t, fn) => rec(t, fn), removeEventListener: () => {},
  body: el('body'), head: el('head'), documentElement: el('html'),
  readyState: 'loading',
}, { get(t, p) { return p in t ? t[p] : mk('document.' + String(p)); }, set(){ return true; } });
globalThis.window = globalThis;
globalThis.alert = () => {}; globalThis.confirm = () => true; globalThis.prompt = () => '';
globalThis.addEventListener = (t, fn) => rec(t, fn);
globalThis.removeEventListener = () => {};
globalThis.requestAnimationFrame = () => 1;   // 読み込み時のみ検査するので回さない
globalThis.cancelAnimationFrame = () => {};
globalThis.setInterval = () => 1; globalThis.clearInterval = () => {};
globalThis.setTimeout = () => 1; globalThis.clearTimeout = () => {};
globalThis.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
globalThis.Image = function(){ return el('img'); };
globalThis.Audio = function(){ return el('audio'); };
try {
__CODE__
} catch (e) {
  console.error('RUNTIME_ERROR:' + (e && e.message ? e.message : String(e)));
  process.exit(3);
}

// 登録されたハンドラを一度だけ叩き、操作時に即死しないかを見る。
// 状態依存で落ちる可能性があるため、ほぼ確実にバグである ReferenceError
// (未定義の変数・関数の参照)だけを報告する。
const ev = (type) => ({
  type, key: 'ArrowRight', code: 'ArrowRight', keyCode: 39, which: 39,
  button: 0, clientX: 10, clientY: 10, offsetX: 10, offsetY: 10,
  touches: [{ clientX: 10, clientY: 10 }], changedTouches: [{ clientX: 10, clientY: 10 }],
  preventDefault(){}, stopPropagation(){}, target: el('target'), currentTarget: el('target'),
});
for (const [type, fn] of HANDLERS.slice(0, 40)) {
  try { fn(ev(type)); }
  catch (e) {
    if (e instanceof ReferenceError) {
      console.error('HANDLER_ERROR:' + type + ': ' + e.message);
      process.exit(4);
    }
  }
}
"""


def _syntax_check(path, content):
    """書き込み直後の即時フィードバック。問題があれば警告文字列、無ければ None。

    小型モデルは「書けた」と思い込んで次に進みやすいので、壊れた構文をその場で
    知らせてフィードバックループを最短にする。subprocess を起こさず純Pythonで
    完結するものだけを対象にする(py / json)。
    """
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".py":
            ast.parse(content)
        elif ext == ".json":
            json.loads(content)
    except SyntaxError as e:
        return f"⚠ 構文エラー: {e.msg} (行 {e.lineno})。直して書き直すこと"
    except json.JSONDecodeError as e:
        return f"⚠ JSONが不正: {e.msg} (行 {e.lineno})。直して書き直すこと"
    except ValueError as e:
        return f"⚠ 構文チェック失敗: {e}"
    return None


class Toolbox:
    """1つの Run(エージェント実行)が使うツール一式。

    subdir: WORKSPACE 配下の追加ルート(例 "run_ab12/sub_0")。"" なら projects/ 直下。
    approve: run_command の実行前承認を求めるか。
    approver: async callable(command, cwd) -> bool。None かつ approve=True ならコンソール承認。
    """

    def __init__(self, subdir="", approve=True, approver=None):
        root = os.path.normpath(os.path.join(WORKSPACE, subdir)) if subdir else WORKSPACE
        if not (root == WORKSPACE or root.startswith(WORKSPACE + os.sep)):
            raise ValueError(f"subdir が projects/ の外を指している: {subdir}")
        self.root = root
        self.approve = approve
        self.approver = approver

    def _safe_path(self, path):
        """root 配下に正規化・限定。外に出る指定は拒否。

        パスは root からの相対として扱う。モデルが自然に "projects/foo" と
        書いても二重にならないよう、先頭の冗長な "projects/" は吸収する。
        """
        path = (path or ".").replace("\\", "/").lstrip("/")
        while path == "projects" or path.startswith("projects/"):
            path = path[len("projects"):].lstrip("/") or "."
        p = os.path.normpath(os.path.join(self.root, path))
        if not (p == self.root or p.startswith(self.root + os.sep)):
            raise ValueError(f"作業ディレクトリの外は禁止: {path}")
        return p

    # ---- 個々のツール実装 ----------------------------------------------------
    def list_dir(self, path="."):
        p = self._safe_path(path)
        if not os.path.exists(p):
            return f"(存在しない: {path})"
        if os.path.isfile(p):
            return f"(ファイル) {path}"
        items = []
        for name in sorted(os.listdir(p)):
            full = os.path.join(p, name)
            items.append(name + ("/" if os.path.isdir(full) else ""))
        return "\n".join(items) or "(空)"

    def read_file(self, path, start_line=None, end_line=None):
        p = self._safe_path(path)
        if not os.path.isfile(p):
            return f"(存在しない: {path})"
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        total = len(lines)
        if start_line or end_line:
            s = max(1, int(start_line or 1))
            e = min(total, int(end_line or total))
            body = "".join(lines[s - 1:e])
            prefix = f"(行 {s}-{e} / 全{total}行)\n"
        else:
            body = "".join(lines)
            prefix = ""
        # コードはヘッダ・import・定義が先頭にあるため「先頭優先+末尾少量」で切る
        if len(body) > 8000:
            body = (body[:6000]
                    + f"\n…(省略: 全{total}行/{len(body)}字。"
                    "start_line/end_line で範囲指定して続きを読める)…\n"
                    + body[-1000:])
        return prefix + body

    def write_file(self, path, content):
        p = self._safe_path(path)
        os.makedirs(os.path.dirname(p) or self.root, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        msg = f"書き込み完了: {path} ({len(content)} 文字)"
        # 構文が壊れていても書き込み自体は通す(モデルが直せるように結果で知らせる)
        warn = _syntax_check(path, content)
        return f"{msg}\n{warn}" if warn else msg

    def edit_file(self, path, old_string, new_string, replace_all=False):
        """既存ファイルの部分置換。全文を書き直させないための主力ツール。

        old_string が見つからない/複数該当する場合は編集せずエラーを返す
        (曖昧なまま置換して意図しない箇所を壊すより、モデルに直させる方が安全)。
        """
        p = self._safe_path(path)
        if not os.path.isfile(p):
            return f"(存在しない: {path}) — 新規作成なら write_file を使うこと"
        if old_string == new_string:
            return "[編集エラー] old_string と new_string が同一です"
        if not old_string:
            return "[編集エラー] old_string が空です(全文置換は write_file を使うこと)"
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        count = content.count(old_string)
        if count == 0:
            return (f"[編集エラー] old_string が {path} に見つかりません。"
                    "read_file で現在の内容を確認し、空白・インデントまで完全に一致させること")
        if count > 1 and not replace_all:
            return (f"[編集エラー] old_string が {count} 箇所に一致し特定できません。"
                    "前後の行を含めて一意になるまで広げるか、replace_all=true を指定すること")
        updated = (content.replace(old_string, new_string) if replace_all
                   else content.replace(old_string, new_string, 1))
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(updated)
        msg = f"編集完了: {path} ({count if replace_all else 1} 箇所を置換)"
        warn = _syntax_check(path, updated)
        return f"{msg}\n{warn}" if warn else msg

    def search_files(self, pattern, path=".", glob=None, max_results=50):
        """作業ディレクトリ配下を正規表現で検索する(grep相当)。

        全文 read_file でコンテキストを潰さずに「どこに何があるか」を掴むためのツール。
        """
        root = self._safe_path(path)
        if not os.path.exists(root):
            return f"(存在しない: {path})"
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return f"[検索エラー] 正規表現が不正: {e}"
        try:
            cap = max(1, min(int(max_results), 200))
        except (TypeError, ValueError):
            cap = 50

        hits, truncated = [], False
        if os.path.isfile(root):
            targets = [root]
        else:
            targets = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in SEARCH_SKIP_DIRS]
                targets.extend(os.path.join(dirpath, fn) for fn in sorted(filenames))

        for full in targets:
            if glob and not fnmatch.fnmatch(os.path.basename(full), glob):
                continue
            try:
                with open(full, "r", encoding="utf-8") as f:
                    for i, line in enumerate(f, 1):
                        if rx.search(line):
                            rel = os.path.relpath(full, self.root).replace("\\", "/")
                            hits.append(f"{rel}:{i}: {line.rstrip()[:200]}")
                            if len(hits) >= cap:
                                truncated = True
                                break
            except (OSError, UnicodeDecodeError):
                continue  # バイナリ・読めないファイルは飛ばす
            if truncated:
                break

        if not hits:
            return f"(一致なし: /{pattern}/)"
        out = "\n".join(hits)
        if truncated:
            out += f"\n…(上限{cap}件で打ち切り。パターンを絞ること)"
        return out

    async def run_command(self, command, working_dir=".", timeout=120):
        # timeout はモデルが指定できる(pip install 等は既定120秒では足りない)。
        # 無限待ちを避けるため MAX_TIMEOUT でクランプする。
        try:
            timeout = max(1, min(int(timeout), MAX_TIMEOUT))
        except (TypeError, ValueError):
            timeout = 120
        if DENY_RE.search(command):
            return f"[拒否] 破壊的コマンドの疑い(denylist): {command}"
        if not self.approve and ESCAPE_RE.search(command):
            return ("[拒否] ワークスペース外参照の疑い(絶対パス/UNC/../環境変数)。"
                    "作業ルートからの相対パスに書き直して再実行して")
        cwd = self._safe_path(working_dir)
        os.makedirs(cwd, exist_ok=True)
        if self.approve:
            ok = await (self.approver(command, cwd) if self.approver
                        else _console_approve(command, cwd))
            if not ok:
                return "[スキップ] ユーザーが実行を承認しなかった"
        proc = None
        try:
            proc = await asyncio.create_subprocess_shell(
                command, cwd=cwd,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                return f"[timeout] {timeout}s 超過: {command}"
        except OSError as e:
            return f"[実行エラー] {command}: {e}"
        finally:
            # timeout・Runキャンセル(CancelledError)いずれでも子プロセスを残さない
            if proc is not None and proc.returncode is None:
                proc.kill()
                try:
                    # 刈り取らないとパイプ未クローズの警告が出る。キャンセル中でも
                    # shield 側が後始末を続けるので、ここでの中断は無視してよい。
                    await asyncio.shield(proc.wait())
                except BaseException:
                    pass
        out = stdout.decode("utf-8", errors="replace")[-4000:]
        err = stderr.decode("utf-8", errors="replace")[-2000:]
        return f"exit={proc.returncode}\n--- stdout ---\n{out}\n--- stderr ---\n{err}"

    def _all_files(self):
        out = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in VERIFY_SKIP_DIRS]
            for fn in filenames:
                if fn.endswith((".pyc", ".pyo")):
                    continue
                out.append(os.path.relpath(os.path.join(dirpath, fn), self.root)
                           .replace("\\", "/"))
        return out

    async def verify_runtime(self, kind=None):
        """HTMLアプリを最小DOMスタブ上で実際に読み込み、起動時に落ちないか検査する。

        エージェントはブラウザで動かして確かめられないため、ここで代わりに走らせる。
        Node が無ければスキップ(None)。問題があれば理由を返す。
        """
        global _node_available
        if kind != "html" or _node_available is False:
            return None
        entry = next((f for f in self._all_files() if f.lower().endswith("index.html")), None)
        if not entry:
            return None
        try:
            with open(os.path.join(self.root, entry), "r",
                      encoding="utf-8", errors="replace") as f:
                html = f.read()
        except OSError:
            return None

        blocks = [m.group(1) for m in _SCRIPT_BLOCK_RE.finditer(html)
                  if not _SCRIPT_SRC_RE.match(m.group(0))]
        code = "\n;\n".join(b for b in blocks if b.strip())
        if not code.strip():
            return None

        ids = sorted(set(_ID_RE.findall(html)))
        tags = sorted({m.lower() for m in re.findall(r"<([a-zA-Z][a-zA-Z0-9]*)", html)})
        harness = (_DOM_HARNESS
                   .replace("__IDS__", json.dumps(ids))
                   .replace("__TAGS__", json.dumps(tags))
                   .replace("__CODE__", code))

        import tempfile
        tmp = os.path.join(tempfile.gettempdir(), f"_agentlab_run_{os.getpid()}.js")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(harness)
            proc = await asyncio.create_subprocess_exec(
                "node", tmp,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
        except FileNotFoundError:
            _node_available = False
            return None
        except (OSError, asyncio.TimeoutError):
            return None
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

        if proc.returncode == 0:
            return None
        err = stderr.decode("utf-8", errors="replace")
        m = re.search(r"HANDLER_ERROR:(.*)", err)
        if m:
            return (f"[finish拒否] {entry} は開けますが、操作したとたんにエラーで止まります: "
                    f"{m.group(1).strip()[:200]}\n"
                    "未定義の変数・関数を参照しています。宣言し忘れがないか確認して直してください。")
        m = re.search(r"RUNTIME_ERROR:(.*)", err)
        detail = (m.group(1) if m else err.strip().splitlines()[-1] if err.strip() else "不明")
        return (f"[finish拒否] {entry} をブラウザで読み込むと起動時にエラーで止まります: "
                f"{detail.strip()[:200]}\n"
                "存在しない要素を参照していないか等を確認し、直してから finish してください。")

    def verify_deliverable(self, kind=None):
        """finish 前の最終ゲート。実行できる成果物が無ければ理由(文字列)を返す。

        「作り方の手順書だけ書いて終わる」失敗を構造的に防ぐための検査。
        問題なければ None。
        """
        files = self._all_files()
        if not files:
            return ("[finish拒否] 成果物が1つもありません。"
                    "説明ではなく、実際に動くファイルを write_file で作ってから finish してください。")
        low = [f.lower() for f in files]
        listing = ", ".join(files[:12])

        def has(suffixes):
            return any(f.endswith(suffixes) for f in low)

        if kind == "html":
            entries = [f for f in files if f.lower().endswith("index.html")]
            if not entries:
                return (f"[finish拒否] index.html がありません(現在: {listing})。"
                        "単一HTMLアプリとして index.html を作ってから finish してください。")
            best_err = ("[finish拒否] index.html に動作するJavaScriptが入っていません。"
                        "実際に動くアプリとして中身を実装してから finish してください。")
            for rel in entries:
                try:
                    with open(os.path.join(self.root, rel), "r",
                              encoding="utf-8", errors="replace") as f:
                        c = f.read()
                except OSError:
                    continue
                if "<script" not in c.lower() or len(c) <= 300:
                    continue
                if _EXTERNAL_RE.search(c):
                    best_err = (f"[finish拒否] {rel} が外部URL(CDN等)を読み込んでいます。"
                                "ネット接続なしで動く必要があるため、外部参照をやめて"
                                "素のJavaScriptで実装し直してください。")
                    continue
                if not _BOOT_RE.search(c):
                    best_err = (f"[finish拒否] {rel} は読み込み時に何も起動していません"
                                "(初期化・描画・ループの呼び出しが見つからない)。"
                                "開いた瞬間に動き出すよう、スクリプト末尾で初期化処理を"
                                "呼ぶか load イベントで開始してください。")
                    continue
                return None
            return best_err

        if kind == "exe":
            if not has((".exe",)):
                return (f"[finish拒否] .exe が生成されていません(現在: {listing})。"
                        "PyInstaller でビルドし dist/ に exe を作ってから finish してください。")
            return None

        if kind == "script":
            if not has((".bat", ".cmd")):
                return (f"[finish拒否] 起動用の run.bat がありません(現在: {listing})。"
                        "ダブルクリックで起動できる run.bat を作ってから finish してください。")
            # ランチャー自身は「本体」に数えない
            body = tuple(e for e in CODE_EXT if e not in (".bat", ".cmd", ".exe"))
            if not has(body):
                return (f"[finish拒否] 起動スクリプトはありますが実行するコード本体がありません"
                        f"(現在: {listing})。本体を実装してから finish してください。")
            return None

        # 形式指定なし: 説明ドキュメントだけで終わっていないかだけを見る
        if not has(CODE_EXT):
            return (f"[finish拒否] 説明ドキュメントしかありません(現在: {listing})。"
                    "手順書ではなく、実際に動くコードを作ってから finish してください。")
        return None

    async def _check_js_after_write(self, path):
        """書き込み直後のファイルを読み直してJS構文を検査する。"""
        if not path:
            return None
        try:
            p = self._safe_path(path)
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                return await _js_syntax_check(path, f.read())
        except (OSError, ValueError):
            return None

    async def run(self, name, args):
        """ツール名 + 引数(dict)を実行して文字列結果を返す。finish は None。

        戻り値は RESULT_CAP で一括クランプする(read_file等の個別キャップの最終防衛線。
        巨大な結果はそのまま会話履歴に恒久挿入されるため)。
        """
        if name == "finish":
            return None  # 呼び出し側で終了扱い
        if not isinstance(args, dict):
            return f"[引数エラー] {name}: 引数がオブジェクトでない: {args!r}"
        try:
            if name == "run_command":
                result = await self.run_command(**args)
            else:
                fn = {"list_dir": self.list_dir, "read_file": self.read_file,
                      "write_file": self.write_file, "edit_file": self.edit_file,
                      "search_files": self.search_files}.get(name)
                if fn is None:
                    return (f"[不正ツール] 未知のツール名: {name}(利用可: list_dir, read_file, "
                            "search_files, write_file, edit_file, run_command, finish)")
                result = fn(**args)
        except TypeError as e:
            return f"[引数エラー] {name}: {e}"
        except ValueError as e:
            return f"[パスエラー] {name}: {e}"
        except Exception as e:
            return f"[実行エラー] {name}: {e}"
        result = str(result)
        # HTML/JS はエージェントが実行して確かめられないので、書いた直後に構文だけ検査する
        # (write_file/edit_file の同期チェックは py/json 用。ここは非同期の Node 検査)
        if name in ("write_file", "edit_file") and not result.startswith(("[", "(")):
            warn = await self._check_js_after_write(args.get("path", ""))
            if warn:
                result += "\n" + warn
        if len(result) > RESULT_CAP:
            result = result[:RESULT_CAP] + f"\n…(結果が長いため{RESULT_CAP}字で切り詰め)…"
        return result


async def _console_approve(command, cwd):
    print(f"\n[承認要求] 実行しますか?\n  $ {command}\n  (cwd={os.path.relpath(cwd, BASE)})")
    ans = input("  y/N > ").strip().lower()
    return ans in ("y", "yes")


# ---- function-calling スキーマ(Ollama native /api/chat 互換) ----------------
TOOLS_SCHEMA = [
    {"type": "function", "function": {
        "name": "list_dir", "description": "作業ディレクトリ配下の一覧",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "作業ルートからの相対パス"}}}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "ファイル内容を読む(長いファイルは省略される。start_line/end_lineで範囲指定可)",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "description": "読み始める行(1始まり・省略可)"},
            "end_line": {"type": "integer", "description": "読み終わる行(両端含む・省略可)"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "search_files",
        "description": "作業ディレクトリ配下を正規表現で検索する(grep相当)。"
                       "どのファイルに何があるかを、全文を読まずに把握するために使う",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "検索する正規表現"},
            "path": {"type": "string", "description": "検索対象のディレクトリ/ファイル。既定 '.'"},
            "glob": {"type": "string", "description": "ファイル名フィルタ(例 '*.py')。省略可"},
            "max_results": {"type": "integer", "description": "最大件数。既定50"}},
            "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "ファイルを新規作成する(既存ファイルの一部を直す場合は edit_file を使うこと)。"
                       "py/json は書き込み後に構文チェックされる",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "既存ファイルの一部だけを置換する。既存コードを壊さずに直せるので、"
                       "ファイル修正では write_file より必ずこちらを優先すること。"
                       "old_string は周囲の行を含めて対象ファイル内で一意になるように指定する",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string", "description": "置換前の文字列(空白・インデントまで完全一致)"},
            "new_string": {"type": "string", "description": "置換後の文字列"},
            "replace_all": {"type": "boolean", "description": "一致する全箇所を置換する。既定false"}},
            "required": ["path", "old_string", "new_string"]}}},
    {"type": "function", "function": {
        "name": "run_command", "description": "作業ディレクトリ配下でシェルコマンドを実行(導入/実行/テスト)",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"},
            "working_dir": {"type": "string", "description": "作業ルートからの相対。既定 '.'"},
            "timeout": {"type": "integer",
                        "description": f"秒。既定120、上限{MAX_TIMEOUT}。pip install 等の"
                                       "時間がかかるコマンドでは長めに指定すること"}},
            "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "finish", "description": "目標達成時に呼ぶ。要約を渡す",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string"}}, "required": ["summary"]}}},
]

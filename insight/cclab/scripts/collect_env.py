"""Claude Code 環境を読み取り専用で調査し、環境 JSON を返す(lab.py から使う。単体実行は出力先を引数で渡す)。

秘密情報の扱い:
- .credentials.json / *secrets*.json / .env は開かない(存在とサイズだけ記録)
- settings の env は「変数名だけ」記録し値は捨てる
- hooks の command はパスを含むので記録するが、トークン様の文字列は伏せる
"""
import json, os, platform, re, shutil, subprocess, sys, glob, datetime
from pathlib import Path

HOME = Path.home()
CLAUDE = Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude")
# managed 設定の置き場所は OS ごとに決まっている(公式 S-04 / M-01)
MANAGED = Path({"win32": "C:/Program Files/ClaudeCode", "darwin": "/Library/Application Support/ClaudeCode"}.get(sys.platform, "/etc/claude-code"))
SECRET_NAME = re.compile(r"(credential|secret|\.env$|\.pem$|\.key$|auth\.json)", re.I)
TOKEN_LIKE = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{8,}|[A-Za-z0-9_-]{40,})")
SECRET_KV = re.compile(r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key|authorization)(\"?\s*[=:]\s*\"?|\s+)([^\s\",}]+)")
AUTH_VALUE = re.compile(r"(?i)\b(bearer|basic)\s+\S+")


def redact(s):
    if not isinstance(s, str):
        return s
    s = AUTH_VALUE.sub(r"\1 <REDACTED>", s)
    return TOKEN_LIKE.sub("<REDACTED>", SECRET_KV.sub(r"\1\2<REDACTED>", s))


def stat(p):
    p = Path(p)
    if not p.exists():
        return {"path": str(p), "exists": False}
    st = p.stat()
    return {"path": str(p), "exists": True, "is_dir": p.is_dir(), "size": st.st_size,
            "mtime": datetime.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")}


def load_json(p):
    if SECRET_NAME.search(Path(p).name):
        return None  # 秘密ファイルは開かない
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return None


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception as e:
        return f"<error: {e.__class__.__name__}>"


def settings_summary(p):
    d = load_json(p)
    info = stat(p)
    if d is None:
        return info
    info["top_level_keys"] = sorted(d.keys())
    if "env" in d:
        info["env_names"] = sorted(d["env"].keys())  # 値は記録しない
    if "permissions" in d:
        perm = d["permissions"]
        info["permissions"] = {k: (len(v) if isinstance(v, list) else v) for k, v in perm.items()}
        for k in ("deny", "ask", "allow"):
            if isinstance(perm.get(k), list):
                info.setdefault("permission_rules", {})[k] = [redact(r) for r in perm[k]]
    if "hooks" in d:
        hooks = {}
        for ev, entries in d["hooks"].items():
            lst = []
            for e in entries:
                for h in e.get("hooks", []):
                    lst.append({"matcher": e.get("matcher", ""), "type": h.get("type"),
                                "command": redact(h.get("command", ""))[:200], "timeout": h.get("timeout")})
            hooks[ev] = lst
        info["hooks"] = hooks
    for k in ("model", "language", "autoMemoryEnabled", "cleanupPeriodDays"):
        if k in d:
            info[k] = d[k]
    if "enabledPlugins" in d:
        info["enabledPlugins"] = d["enabledPlugins"]
    return info


def list_md(dirpath, pattern="*.md"):
    return sorted(str(p.relative_to(dirpath)) for p in Path(dirpath).glob(pattern)) if Path(dirpath).exists() else []


def frontmatter(p):
    try:
        t = Path(p).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}
    m = re.match(r"---\n(.*?)\n---", t, re.S)
    fm = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line and not line.startswith(" "):
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip()[:160]
    fm["_bytes"] = len(t.encode("utf-8"))
    return fm


def collect():
    env = {"collected_at": datetime.datetime.now().isoformat(timespec="seconds"),
           "evidence_class": "B", "collector": "cc-session-lab/scripts/collect_env.py"}
    env["os"] = {"platform": sys.platform, "release": platform.release(), "version": platform.version(),
                 "machine": platform.machine(), "python": platform.python_version()}
    exe = shutil.which("claude")
    env["claude_code"] = {"version": run([exe, "--version"]) if exe else None, "executable": exe,
                          "node": run(["node", "--version"])}
    # 秘密を含まないと確認した変数だけ値を残す(それ以外は名前のみ)
    env["claude_code"]["runtime"] = {k: os.environ.get(k) for k in
                                     ("CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_DESKTOP_APP_VERSION", "CLAUDE_EFFORT",
                                      "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_CODE_USE_POWERSHELL_TOOL",
                                      "DISABLE_MICROCOMPACT", "CLAUDE_CODE_SESSION_ATTENDED")}
    cj = load_json(CLAUDE / ".claude.json") or load_json(HOME / ".claude.json") or {}
    env["claude_code"]["installMethod"] = cj.get("installMethod")
    env["claude_code"]["numStartups"] = cj.get("numStartups")
    env["claude_code"]["global_mcpServers"] = sorted((cj.get("mcpServers") or {}).keys())
    env["claude_code"]["projects_with_local_mcp"] = sum(
        1 for v in (cj.get("projects") or {}).values() if isinstance(v, dict) and v.get("mcpServers"))
    env["claude_code"]["feature_flag_count"] = len(cj.get("cachedGrowthBookFeatures") or {})
    env["env_var_names"] = sorted(k for k in os.environ if re.match(r"(CLAUDE|ANTHROPIC|MCP|OTEL|DISABLE_|BASH_|MAX_)", k))

    env["settings"] = {
        "managed": settings_summary(MANAGED / "managed-settings.json"),
        "user": settings_summary(CLAUDE / "settings.json"),
        "user_local": settings_summary(CLAUDE / "settings.local.json"),
    }
    env["instruction_files"] = {
        "user_CLAUDE.md": stat(CLAUDE / "CLAUDE.md"),
        "managed_CLAUDE.md": stat(MANAGED / "CLAUDE.md"),
        "user_rules_dir": stat(CLAUDE / "rules"),
    }
    env["agents"] = {n: frontmatter(CLAUDE / "agents" / n) for n in list_md(CLAUDE / "agents")}
    env["commands"] = list_md(CLAUDE / "commands")
    env["user_skills"] = {}
    for sk in sorted((CLAUDE / "skills").glob("*/SKILL.md")):
        fm = frontmatter(sk)
        env["user_skills"][sk.parent.name] = {"name": fm.get("name"), "bytes": fm["_bytes"],
                                              "description_chars": len(fm.get("description", ""))}
    ip = load_json(CLAUDE / "plugins" / "installed_plugins.json") or {}
    env["plugins"] = {"installed": sorted((ip.get("plugins") or ip).keys()) if isinstance(ip, dict) else [],
                      "marketplaces": sorted((load_json(CLAUDE / "plugins" / "known_marketplaces.json") or {}).keys())}
    plugin_skills = glob.glob(str(CLAUDE / "plugins" / "**" / "SKILL.md"), recursive=True)
    env["plugins"]["SKILL_md_files_on_disk"] = len(plugin_skills)

    proj = CLAUDE / "projects"
    jsonl = list(proj.glob("*/*.jsonl"))
    env["sessions"] = {"project_dirs": sum(1 for p in proj.iterdir() if p.is_dir()), "session_jsonl": len(jsonl),
                       "subagent_jsonl": len(list(proj.glob("*/*/subagents/*.jsonl"))),
                       "total_bytes": sum(p.stat().st_size for p in jsonl)}
    env["dirs"] = {n: stat(CLAUDE / n) for n in
                   ["projects", "file-history", "shell-snapshots", "session-env", "sessions", "debug", "logs",
                    "plugins", "skills", "agents", "commands", "hooks", "todos", "plans", "statsig", "ide"]}
    env["secret_files_not_opened"] = [stat(CLAUDE / ".credentials.json") | {"note": "存在のみ確認。内容は読まない"}]
    env["git_global_hooksPath"] = run(["git", "config", "--global", "core.hooksPath"])
    env["unconfirmed"] = ["Desktop アプリ本体のバージョン(CLI と別)", "managed CLAUDE.md の有無(上記 stat 参照)",
                          "サーバー側のシステムプロンプト全文(ローカル記録の範囲のみ観測)"]
    env["managed_dir"] = str(MANAGED)
    return env


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "environment.json")
    out.write_text(json.dumps(collect(), ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {out}")

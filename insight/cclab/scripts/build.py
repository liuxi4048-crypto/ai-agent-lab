"""環境・トレース・根拠・用語集をまとめ、ダッシュボード一式を出力先に書き出す。

出力: index.html(ローカルで開く)、artifact.html(Artifact 公開用)、data.js、app 一式、data/*.json
file:// で開いても fetch の CORS に掛からないよう、データは script(window.CC_DATA)として読ませる。
"""
import json
import re
import shutil
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
APP_FILES = ["index.html", "app.js", "graph.js", "model.js", "style.css"]


def load(name):
    return json.loads((SKILL / "data" / name).read_text(encoding="utf-8"))


def build(out, env, trace, auto_facts, token, extra_facts=(), experiments=()):
    out = Path(out)
    (out / "data").mkdir(parents=True, exist_ok=True)
    by_suffix = {t: e["id"] for e in experiments for t in e.get("tools", [])}
    for t in trace["tools"]:  # 対応表にある呼び出しだけ上書きし、抽出時の EXP: マーカー由来の値は残す
        t["exp"] = by_suffix.get(t["id"][-8:], t.get("exp"))
    for s in trace["subagents"]:
        for e in s["events"]:  # サブエージェントのツール名一覧は先頭だけ残す
            if e["kind"] == "request_shape":
                e["detail"]["tool_names"] = e["detail"]["tool_names"][:60]
    facts, unconfirmed = [], []
    for f in ("facts_official_config.json", "facts_official_runtime.json"):
        d = load(f)
        facts += [x | {"cls": "A", "evidence": x["url"]} for x in d["facts"]]
        unconfirmed += d["unconfirmed"]
    # 追加所見の分類は画面で HTML に使うので A〜E に限る
    extra = [f for f in extra_facts if f.get("cls") in ("A", "B", "C", "D", "E")]
    facts += load("findings_reference.json")["facts"] + list(auto_facts) + extra
    data = {"env": env, "trace": trace, "experiments": list(experiments), "facts": facts,
            "unconfirmed": unconfirmed, "glossary": load("glossary.json"), "token": token}
    # ツール出力の文字化け(元が CP932 など)は U+FFFD になっているので「?」に置き換える
    text = json.dumps(data, ensure_ascii=False).replace("�", "?")
    (out / "data.js").write_text("window.CC_DATA = " + text + ";\n", encoding="utf-8")
    for name in APP_FILES:
        shutil.copyfile(SKILL / "app" / name, out / name)
    # Artifact 公開用: 公開時に doctype/head/body が付くので、index.html の中身だけを取り出す
    html = (SKILL / "app" / "index.html").read_text(encoding="utf-8")
    head = re.search(r"<head>(.*?)</head>", html, re.S).group(1)
    body = re.search(r"<body>(.*?)</body>", html, re.S).group(1)
    keep = "\n".join(l for l in head.splitlines() if "<title>" in l or "stylesheet" in l)
    (out / "artifact.html").write_text(keep + "\n" + body.strip() + "\n", encoding="utf-8")
    for name, obj in (("environment", env), ("trace", trace), ("token_report", token), ("findings_auto", auto_facts)):
        (out / "data" / f"{name}.json").write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"out": str(out), "index": str(out / "index.html"), "facts": len(facts), "tools": len(trace["tools"]),
            "api_calls": len(trace["api_calls"]), "subagents": len(trace["subagents"])}

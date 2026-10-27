import { useCallback, useEffect, useState } from "react";
import { Microscope, Loader2, RefreshCw, ExternalLink, BookOpen } from "lucide-react";
import { fetchInsightRuns, fetchInsightRun, fetchClaudeSessions, buildClaudeSession } from "../../api.js";
import { GlossaryProvider, GlossaryList, TermText } from "./Terms.jsx";
import RunInsight from "./RunInsight.jsx";
import AnalystPanel from "./AnalystPanel.jsx";
import { fmtAgo, MODE_LABEL } from "../../derive.js";

const TABS = [
  { key: "runs", label: "ai-agent-lab の実行" },
  { key: "claude", label: "Claude Code のセッション" },
  { key: "glossary", label: "用語集" },
];

/**
 * インサイト(右ペイン全面): 実行記録を可視化し、ローカルLLMの分析エージェントに解説させる。
 * - ai-agent-lab の Run(runs/*.json)… 流れ図・時間軸の再生・トークン・改善候補
 * - Claude Code のセッション … cc-session-lab のダッシュボードを埋め込み表示
 */
export default function InsightView({ models }) {
  const [tab, setTab] = useState("runs");
  const [focusTerm, setFocusTerm] = useState(null);
  const openTerm = useCallback((id) => { setTab("glossary"); setFocusTerm(id); }, []);
  return (
    <GlossaryProvider onOpenTerm={openTerm}>
      <div className="flex h-full min-h-0 flex-col overflow-y-auto px-4 py-8">
        <div className="mx-auto w-full max-w-5xl">
          <div className="flex items-center gap-2.5">
            <Microscope size={24} className="text-blue-400" />
            <h2 className="text-2xl font-bold text-slate-100">インサイト</h2>
          </div>
          <p className="mt-2 text-sm text-slate-400">実行の中で何が起きたかを、記録から再現して見る。</p>
          <nav className="mt-5 flex flex-wrap gap-1" role="tablist">
            {TABS.map((t) => (
              <button key={t.key} role="tab" aria-selected={tab === t.key} onClick={() => setTab(t.key)}
                className={`rounded-lg px-3 py-1.5 text-[13px] ${tab === t.key ? "bg-blue-600 text-white" : "text-slate-400 hover:bg-slate-800 hover:text-slate-200"}`}>
                {t.key === "glossary" && <BookOpen size={13} className="mr-1 inline" />}{t.label}
              </button>
            ))}
          </nav>
          <div className="mt-6">
            {tab === "runs" && <RunsTab models={models} />}
            {tab === "claude" && <ClaudeTab models={models} />}
            {tab === "glossary" && <GlossaryList focusId={focusTerm} />}
          </div>
        </div>
      </div>
    </GlossaryProvider>
  );
}

function RunsTab({ models }) {
  const [runs, setRuns] = useState(null);
  const [id, setId] = useState(null);
  const [trace, setTrace] = useState(null);
  const [error, setError] = useState(null);
  const [reload, setReload] = useState(0);   // 更新ボタンで選択中の Run の詳細も取り直す
  const load = () => fetchInsightRuns().then((d) => { setRuns(d.runs); setId((cur) => cur ?? d.runs.find((r) => r.mode !== "bench")?.id ?? d.runs[0]?.id); setReload((n) => n + 1); }).catch((e) => setError(e.message));
  useEffect(() => { load(); }, []);
  useEffect(() => {
    if (!id) return;
    let stale = false;   // 取得中に別の Run を選んだら、古い応答は捨てる
    setTrace(null); setError(null);
    fetchInsightRun(id).then((t) => { if (!stale) setTrace(t); }).catch((e) => { if (!stale) setError(e.message); });
    return () => { stale = true; };
  }, [id, reload]);
  if (!runs) return <p className="text-[13px] text-slate-500">{error ?? "読み込み中…"}</p>;
  if (!runs.length) return <p className="text-[13px] text-slate-500">まだ Run の記録がありません。タスクを実行すると、ここで中身を見られます。</p>;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <label className="text-[13px] text-slate-400" htmlFor="insight-run">Run</label>
        <select id="insight-run" value={id ?? ""} onChange={(e) => setId(e.target.value)}
          className="min-w-0 max-w-full flex-1 rounded-md border border-slate-700 bg-slate-900 px-2 py-1.5 text-[13px]">
          {runs.map((r) => (
            <option key={r.id} value={r.id}>{fmtAgo(r.created_at)} ・ {MODE_LABEL[r.mode] ?? r.mode} ・ {r.status} ・ {r.task}</option>
          ))}
        </select>
        <button onClick={load} className="rounded-lg border border-slate-700 p-2 text-slate-300 transition-colors hover:border-slate-500 hover:text-white" title="一覧を更新" aria-label="一覧を更新"><RefreshCw size={13} /></button>
      </div>
      {error && <p className="text-[13px] text-red-400">{error}</p>}
      {trace ? (
        <>
          <RunInsight trace={trace} />
          <AnalystPanel source="run" id={trace.run.id} models={models} />
        </>
      ) : !error && <p className="text-[13px] text-slate-500"><Loader2 size={13} className="mr-1 inline animate-spin" />解析中…</p>}
    </div>
  );
}

function ClaudeTab({ models }) {
  const [data, setData] = useState(null);
  const [sid, setSid] = useState("");
  const [built, setBuilt] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  useEffect(() => { fetchClaudeSessions().then((d) => { setData(d); setSid(d.sessions[0]?.id ?? ""); }).catch((e) => setError(e.message)); }, []);
  const build = () => {
    setBusy(true); setError(null); setBuilt(null);
    buildClaudeSession(sid).then(setBuilt).catch((e) => setError(e.message)).finally(() => setBusy(false));
  };
  // 作成中はセッションを変えられないようにして、別セッションの結果が混ざらないようにする
  if (!data) return <p className="text-[13px] text-slate-500">{error ?? "読み込み中…"}</p>;
  if (!data.available) return <p className="text-[13px] text-slate-500">この PC には Claude Code のセッション記録(~/.claude/projects)がありません。</p>;
  return (
    <div className="space-y-4">
      <p className="text-[13px] text-slate-400">
        <TermText text="Claude Code のトランスクリプト(JSONL)から、処理の流れ・ツール呼び出し・コンテキスト・サブエージェント・トークン改善の候補を可視化します。記録の本文は保存せず、字数と時刻だけを使います。" />
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <label className="text-[13px] text-slate-400" htmlFor="insight-cc">セッション</label>
        <select id="insight-cc" value={sid} disabled={busy} onChange={(e) => { setSid(e.target.value); setBuilt(null); }}
          className="min-w-0 flex-1 rounded-md border border-slate-700 bg-slate-900 px-2 py-1.5 text-[13px]">
          {data.sessions.map((s) => <option key={s.id} value={s.id}>{s.modified} ・ {s.id.slice(0, 8)} ・ {s.size_kb} KB ・ …{s.project.slice(-24)}</option>)}
        </select>
        <button onClick={build} disabled={busy || !sid}
          className="flex items-center gap-1.5 rounded-lg bg-blue-600 px-3 py-1.5 text-[13px] font-semibold text-white hover:bg-blue-500 disabled:opacity-60">
          {busy && <Loader2 size={14} className="animate-spin" />}{busy ? "作成中…" : "可視化する"}
        </button>
        {built && <a href={built.url} target="_blank" rel="noopener" className="flex items-center gap-1 text-[13px] text-blue-400 hover:underline"><ExternalLink size={13} />別タブで開く</a>}
      </div>
      {error && <p className="text-[13px] text-red-400">{error}</p>}
      {built && (
        <>
          {built.token_top.length > 0 && (
            <div className="rounded-xl border border-slate-800 bg-slate-900 p-4 text-[13px]">
              <div className="mb-1 font-semibold text-slate-200">トークン改善の上位(概算)</div>
              {built.token_top.map((c) => <div key={c.key} className="text-slate-300">・{c.title}(約 {c.est_tokens.toLocaleString()})— <span className="text-slate-500">{c.action}</span></div>)}
            </div>
          )}
          <iframe title="Claude Code セッションのダッシュボード" src={built.url} className="h-[78vh] w-full rounded-xl border border-slate-800 bg-slate-950" />
          <AnalystPanel source="claude" id={built.session} models={models} />
        </>
      )}
    </div>
  );
}

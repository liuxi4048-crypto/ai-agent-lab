import { useEffect, useMemo, useState } from "react";
import { Play, Pause, SkipBack, SkipForward, ChevronsLeft } from "lucide-react";
import { TermText } from "./Terms.jsx";
// トークン数などの整数を桁区切りで出す(derive.js の fmtNum は小数1桁用)
const fmtNum = (v) => (v == null ? "—" : Number(v).toLocaleString("ja-JP"));

// エージェントの種類ごとの色と説明(流れ図・時間軸で共通)
const KIND = {
  task: { c: "#64748b", label: "タスク" }, planner: { c: "#a78bfa", label: "計画役 planner" },
  subagent: { c: "#38bdf8", label: "子エージェント subagent" }, aggregator: { c: "#f59e0b", label: "集約役 aggregator" },
  answer: { c: "#22c55e", label: "最終回答 answer" }, coder: { c: "#38bdf8", label: "コーディング coder" },
  round: { c: "#94a3b8", label: "批評の1往復 round" }, author: { c: "#38bdf8", label: "作成役 author" },
  reviewer: { c: "#f472b6", label: "批評役 reviewer" }, merger: { c: "#f59e0b", label: "統合役 merger" },
};
const kindOf = (k) => KIND[k] ?? { c: "#94a3b8", label: k };
const STATUS_C = { done: "#22c55e", error: "#ef4444", cancelled: "#64748b", running: "#eab308", generating: "#eab308", thinking: "#eab308" };
const fmtS = (s) => (s == null ? "—" : s >= 60 ? `${Math.floor(s / 60)}分${Math.round(s % 60)}秒` : `${s.toFixed(1)}秒`);

/** 1つの Run のインサイト: 要点 → 流れ図 → 時間軸の再生 → トークン → 選んだエージェントの詳細 → 改善候補 */
export default function RunInsight({ trace }) {
  const { run, nodes, stats, candidates } = trace;
  const [sel, setSel] = useState(null);
  const [t, setT] = useState(null);           // 再生中の時刻(秒)。null は再生前
  useEffect(() => { setSel(null); setT(null); }, [run.id]);
  const byId = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);
  const active = useMemo(() => new Set(t == null ? [] : nodes.filter((n) => n.start != null && n.start <= t && (n.end == null || t < n.end)).map((n) => n.id)), [nodes, t]);
  const selNode = sel ? byId.get(sel) : null;

  return (
    <div className="space-y-4">
      <Stats run={run} stats={stats} />
      <Section title="エージェントの流れ" note="四角 = エージェント1体、矢印 = 親が子に作業を渡す向き。矢印にマウスを乗せると何を渡しているかが出ます。再生中は動いているエージェントが光ります。">
        <FlowGraph nodes={nodes} byId={byId} active={active} sel={sel} onSel={setSel} />
      </Section>
      <Section title="時間軸と再生" note="横軸は Run 開始からの経過秒。棒1本 = エージェント1体の動いていた時間。棒が縦に重なっている時間は、同時に動いていたことを表します。">
        <Timeline nodes={nodes} t={t} setT={setT} sel={sel} onSel={setSel} byId={byId} />
      </Section>
      <Section title="トークンと速度" note="棒の長さ = そのエージェントが生成したトークン数。右の数字は所要時間で割った概算の生成速度(読み込み待ちを含むので実際より低め)。">
        <TokenBars nodes={nodes} onSel={setSel} sel={sel} />
      </Section>
      {selNode && <NodeDetail node={selNode} parent={byId.get(selNode.parent)} onClose={() => setSel(null)} />}
      <Section title="改善の候補" note="記録から機械的に見つけたもの。根拠の数字は実測です。">
        {candidates.length ? candidates.map((c) => (
          <div key={c.key} className={`mb-2 rounded-r-lg border-l-4 bg-slate-900 px-4 py-3 ${c.level === "high" ? "border-red-500" : c.level === "mid" ? "border-amber-500" : "border-slate-500"}`}>
            <div className="text-[13px] font-semibold text-slate-100">{c.title}</div>
            <div className="text-[13px] text-slate-300"><TermText text={c.evidence} /></div>
            <div className="text-[12px] text-slate-500">対策: <TermText text={c.action} /></div>
          </div>
        )) : <p className="text-[13px] text-slate-500">目立つ候補はありません。</p>}
      </Section>
    </div>
  );
}

function Section({ title, note, children }) {
  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900 p-4">
      <h3 className="text-sm font-semibold text-slate-100">{title}</h3>
      {note && <p className="mb-2 mt-0.5 text-[12px] text-slate-500">{note}</p>}
      {children}
    </section>
  );
}

function Stats({ run, stats }) {
  const cards = [
    [fmtS(run.duration), "所要時間"], [fmtNum(stats.tokens), "生成トークンの合計"], [stats.nodes, "エージェント数"],
    [stats.max_parallel, "同時実行数(最大)"], [`${stats.tool_calls}(失敗 ${stats.tool_errors})`, "ツール呼び出し"],
    [stats.compactions, "履歴圧縮"],
  ];
  return (
    <div>
      <p className="mb-2 text-[13px] text-slate-300">
        <span className="font-semibold">{run.mode}</span> モード ・ 状態 {run.status} ・ モデル {run.model_tag || run.model} ・ <TermText text={run.task} />
      </p>
      <div className="grid gap-2 [grid-template-columns:repeat(auto-fit,minmax(130px,1fr))]">
        {cards.map(([v, k]) => (
          <div key={k} className="rounded-lg border border-slate-800 bg-slate-900 px-3 py-2">
            <div className="text-lg font-bold tabular-nums text-slate-100">{v}</div>
            <div className="text-xs text-slate-500"><TermText text={k} /></div>
          </div>
        ))}
      </div>
    </div>
  );
}

/** 親子の木を左から右へ描く。深さ = 列、同じ深さの中は開始時刻の順 */
function FlowGraph({ nodes, byId, active, sel, onSel }) {
  const [hoverEdge, setHoverEdge] = useState(null);
  const layout = useMemo(() => {
    const depth = new Map();
    const d = (n) => {
      if (depth.has(n.id)) return depth.get(n.id);
      const p = byId.get(n.parent);
      const v = p && p.id !== n.id ? d(p) + 1 : 0;
      depth.set(n.id, v);
      return v;
    };
    nodes.forEach(d);
    const cols = new Map();
    [...nodes].sort((a, b) => (a.start ?? 0) - (b.start ?? 0)).forEach((n) => {
      const k = depth.get(n.id);
      cols.set(k, [...(cols.get(k) ?? []), n.id]);
    });
    const pos = new Map();
    for (const [k, ids] of cols) ids.forEach((id, i) => pos.set(id, { x: 20 + k * 230, y: 20 + i * 66 }));
    const H = Math.max(...[...cols.values()].map((c) => c.length)) * 66 + 30;
    return { pos, W: (Math.max(...cols.keys()) + 1) * 230 + 20, H };
  }, [nodes, byId]);
  const W0 = 180, H0 = 44;
  return (
    <div className="relative overflow-auto rounded-lg bg-slate-950/60">
      <svg width={layout.W} height={layout.H} role="img" aria-label="エージェントの親子関係">
        <defs><marker id="ins-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0L10,5L0,10z" fill="#475569" /></marker></defs>
        {nodes.filter((n) => layout.pos.has(n.parent)).map((n) => {
          const a = layout.pos.get(n.parent), b = layout.pos.get(n.id);
          const x1 = a.x + W0, y1 = a.y + H0 / 2, x2 = b.x, y2 = b.y + H0 / 2, mx = (x1 + x2) / 2;
          const on = active.has(n.id) || hoverEdge === n.id;
          return (
            <g key={n.id} onMouseEnter={() => setHoverEdge(n.id)} onMouseLeave={() => setHoverEdge(null)} className="cursor-help">
              <path d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`} fill="none" stroke={on ? "#60a5fa" : "#334155"} strokeWidth={on ? 2.5 : 1.5} markerEnd="url(#ins-arrow)" />
              <path d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`} fill="none" stroke="transparent" strokeWidth="14"><title>{n.edge}</title></path>
            </g>
          );
        })}
        {nodes.map((n) => {
          const p = layout.pos.get(n.id); const k = kindOf(n.kind); const on = active.has(n.id);
          return (
            <g key={n.id} transform={`translate(${p.x},${p.y})`} onClick={() => onSel(n.id)} className="cursor-pointer" role="button" tabIndex={0}
              onKeyDown={(e) => e.key === "Enter" && onSel(n.id)}>
              <rect width={W0} height={H0} rx="8" fill="#0f172a" stroke={sel === n.id ? "#c084fc" : on ? "#60a5fa" : k.c} strokeWidth={sel === n.id || on ? 2.5 : 1.3} />
              <rect width="5" height={H0} rx="2" fill={STATUS_C[n.status] ?? "#64748b"} />
              <text x="12" y="18" fill="#e2e8f0" fontSize="12" fontWeight="600">{k.label.split(" ")[0]}</text>
              <text x="12" y="34" fill="#94a3b8" fontSize="11">{(n.model || "").slice(0, 18)} ・ {fmtNum(n.tokens)} tok</text>
            </g>
          );
        })}
      </svg>
      <p className="px-2 py-1.5 text-[12px] text-slate-400">
        {hoverEdge ? <>矢印: <b className="text-slate-200">{kindOf(byId.get(byId.get(hoverEdge)?.parent)?.kind).label} → {kindOf(byId.get(hoverEdge)?.kind).label}</b> … {byId.get(hoverEdge)?.edge}</>
          : "左の帯の色 = 状態(緑 完了 / 赤 エラー / 黄 実行中 / 灰 中断)。クリックで詳細。"}
      </p>
    </div>
  );
}

/** ガントチャート + 再生。再生は「開始・終了」の出来事を順にたどる */
function Timeline({ nodes, t, setT, sel, onSel, byId }) {
  const rows = useMemo(() => nodes.filter((n) => n.kind !== "task" && n.start != null).sort((a, b) => a.start - b.start), [nodes]);
  const events = useMemo(() => rows.flatMap((n) => [{ t: n.start, n, type: "start" }, ...(n.end != null ? [{ t: n.end, n, type: "end" }] : [])]).sort((a, b) => a.t - b.t), [rows]);
  const tMax = Math.max(1, ...rows.map((n) => n.end ?? n.start));
  const [idx, setIdx] = useState(-1);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  useEffect(() => { setIdx(-1); setPlaying(false); }, [nodes]);
  useEffect(() => { setT(idx >= 0 ? events[idx]?.t ?? null : null); }, [idx, events, setT]);
  useEffect(() => {
    if (!playing) return;
    // 1出来事あたり 1.2 秒 ÷ 速度。最後まで行ったら止める
    const timer = setTimeout(() => setIdx((i) => { if (i >= events.length - 1) { setPlaying(false); return i; } return i + 1; }), 1200 / speed);
    return () => clearTimeout(timer);
  }, [playing, idx, speed, events.length]);
  const cur = events[idx];
  const L = 170, W = 860, rh = 22, x = (s) => L + (W - L - 10) * (s / tMax);

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-2 text-[13px]">
        <button className="rounded border border-slate-700 p-1" onClick={() => { setPlaying(false); setIdx(-1); setT(null); }} title="最初へ" aria-label="最初へ"><ChevronsLeft size={14} /></button>
        <button className="rounded border border-slate-700 p-1" onClick={() => { setPlaying(false); setIdx((i) => Math.max(0, i - 1)); }} title="1つ戻る" aria-label="1つ戻る"><SkipBack size={14} /></button>
        <button className="flex items-center gap-1 rounded bg-blue-600 px-2.5 py-1 font-semibold text-white" onClick={() => { if (idx >= events.length - 1) setIdx(-1); setPlaying((p) => !p); }} disabled={!events.length}>
          {playing ? <Pause size={14} /> : <Play size={14} />} {playing ? "一時停止" : "再生"}
        </button>
        <button className="rounded border border-slate-700 p-1" onClick={() => { setPlaying(false); setIdx((i) => Math.min(events.length - 1, i + 1)); }} title="1つ進む" aria-label="1つ進む"><SkipForward size={14} /></button>
        <label className="flex items-center gap-1">速度
          <select value={speed} onChange={(e) => setSpeed(+e.target.value)} className="rounded border border-slate-700 bg-slate-900 px-1">
            {[0.25, 0.5, 1, 2, 4, 8].map((v) => <option key={v} value={v}>{v}×</option>)}
          </select>
        </label>
        <input type="range" min={-1} max={events.length - 1} value={idx} onChange={(e) => { setPlaying(false); setIdx(+e.target.value); }} className="min-w-40 flex-1 accent-blue-500" aria-label="再生位置" />
        <span className="tabular-nums text-slate-500">{idx + 1}/{events.length}</span>
      </div>
      <div className="min-h-12 rounded-lg bg-slate-950/60 px-3 py-2 text-[13px]">
        {cur ? (
          <>
            <div className="text-slate-200"><b>{cur.t.toFixed(1)}秒</b>: {kindOf(cur.n.kind).label}「{cur.n.title}」が{cur.type === "start" ? "動き始めた" : `終わった(${cur.n.status}・${fmtS(cur.n.dur)}・${fmtNum(cur.n.tokens)} トークン)`}</div>
            {cur.type === "start" && cur.n.edge && (
              <div className="mt-0.5 text-slate-400">矢印 {kindOf(byId.get(cur.n.parent)?.kind).label} → {kindOf(cur.n.kind).label}: <TermText text={cur.n.edge} /></div>
            )}
          </>
        ) : <span className="text-slate-500">再生を押すと、エージェントが動き始めた順・終わった順に1つずつ説明します。</span>}
      </div>
      <div className="mt-2 overflow-x-auto">
        <svg width={W} height={rows.length * rh + 26} role="img" aria-label="エージェントの実行期間">
          {[0, 0.25, 0.5, 0.75, 1].map((f) => (
            <g key={f}><line x1={x(tMax * f)} x2={x(tMax * f)} y1="0" y2={rows.length * rh} stroke="#1e293b" strokeDasharray="2 3" />
              <text x={x(tMax * f)} y={rows.length * rh + 16} fill="#64748b" fontSize="11" textAnchor="middle">{(tMax * f).toFixed(0)}秒</text></g>
          ))}
          {rows.map((n, i) => (
            <g key={n.id} onClick={() => onSel(n.id)} className="cursor-pointer">
              <rect x="0" y={i * rh} width={W} height={rh} fill={sel === n.id ? "#1e293b" : "transparent"} />
              <text x="4" y={i * rh + 15} fill="#cbd5e1" fontSize="12">{kindOf(n.kind).label.split(" ")[0]} {n.title.slice(0, 12)}</text>
              <rect x={x(n.start)} y={i * rh + 4} width={Math.max(3, x(n.end ?? tMax) - x(n.start))} height={rh - 8} rx="3"
                fill={STATUS_C[n.status] ?? "#64748b"} opacity={n.end == null ? 0.5 : 0.85}><title>{`${n.title} ${fmtS(n.dur)}`}</title></rect>
            </g>
          ))}
          {t != null && <line x1={x(t)} x2={x(t)} y1="0" y2={rows.length * rh} stroke="#eab308" strokeWidth="2" />}
        </svg>
      </div>
    </div>
  );
}

function TokenBars({ nodes, onSel, sel }) {
  const rows = nodes.filter((n) => n.tokens > 0).sort((a, b) => b.tokens - a.tokens);
  const mx = Math.max(1, ...rows.map((n) => n.tokens));
  if (!rows.length) return <p className="text-[13px] text-slate-500">トークンの記録がありません。</p>;
  return (
    <div className="space-y-1">
      {rows.map((n) => (
        <button key={n.id} onClick={() => onSel(n.id)} className={`grid w-full grid-cols-[150px_1fr_110px] items-center gap-2 rounded px-1 text-left text-[12px] ${sel === n.id ? "bg-slate-800" : "hover:bg-slate-800/60"}`}>
          <span className="truncate text-slate-300">{kindOf(n.kind).label.split(" ")[0]} {n.title.slice(0, 10)}</span>
          <span className="h-2.5 rounded" style={{ width: `${(100 * n.tokens) / mx}%`, background: kindOf(n.kind).c }} />
          <span className="tabular-nums text-slate-400">{fmtNum(n.tokens)}{n.tps ? ` ・ ${n.tps} tok/s` : ""}</span>
        </button>
      ))}
    </div>
  );
}

function NodeDetail({ node: n, parent, onClose }) {
  return (
    <section className="rounded-xl border border-violet-500/50 bg-slate-900 p-4">
      <div className="flex items-center gap-2">
        <h3 className="text-sm font-semibold text-slate-100">{kindOf(n.kind).label}「{n.title}」</h3>
        <button onClick={onClose} className="ml-auto text-[12px] text-slate-400 hover:text-slate-200">閉じる</button>
      </div>
      <dl className="mt-2 grid grid-cols-[110px_1fr] gap-x-3 gap-y-1 text-[13px]">
        <dt className="text-slate-500">状態</dt><dd>{n.status}{n.stop === "max_iter" ? "(反復の上限で停止)" : ""}</dd>
        <dt className="text-slate-500">モデル</dt><dd>{n.model || "—"}</dd>
        <dt className="text-slate-500">時間</dt><dd>{n.start != null ? `${n.start.toFixed(1)}秒 から ${fmtS(n.dur)}` : "—"}</dd>
        <dt className="text-slate-500">トークン</dt><dd>{fmtNum(n.tokens)}{n.tps ? `(約 ${n.tps} tok/s)` : ""}</dd>
        <dt className="text-slate-500">字数</dt><dd>入力 {fmtNum(n.prompt_chars)} ・ 出力 {fmtNum(n.output_chars)} ・ 思考 {fmtNum(n.think_chars)}</dd>
        {parent && <><dt className="text-slate-500">親からの矢印</dt><dd><TermText text={n.edge} /></dd></>}
        {n.iters > 0 && <><dt className="text-slate-500">反復</dt><dd>{n.iters}{n.max_iter ? ` / 上限 ${n.max_iter}` : ""}</dd></>}
      </dl>
      {n.events.length > 0 && (
        <>
          <h4 className="mt-3 text-[13px] font-semibold text-slate-200">中で起きたこと <span className="font-normal text-slate-500">(<TermText text="順序のみ(時刻なし)" />)</span></h4>
          <ol className="mt-1 max-h-72 list-decimal space-y-0.5 overflow-auto pl-6 text-[13px]">
            {n.events.map((e, i) => {
              const tool = e.type === "tool" ? n.tools[e.tool] : null;
              return (
                <li key={i} className={tool?.ok === false ? "text-red-300" : e.type === "compact" ? "text-amber-300" : e.type === "iter" ? "text-slate-500" : "text-slate-300"}>
                  {tool ? <><b>{tool.name}</b> <span className="text-slate-500">{tool.args}</span> → {tool.result || "—"}</> : e.label}
                </li>
              );
            })}
          </ol>
        </>
      )}
    </section>
  );
}

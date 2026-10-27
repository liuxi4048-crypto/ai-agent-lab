import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { fetchGlossary } from "../../api.js";

/**
 * 用語集の読み込みと、本文中の専門用語に点線を付けてホバー/タップで説明を出す仕組み。
 * <GlossaryProvider> の内側で <TermText text="..."/> を使うと、知っている語の初出だけが印付きになる。
 */
const GlossaryCtx = createContext({ terms: [], byId: new Map(), re: null, keyToId: new Map(), open: () => {} });

export function GlossaryProvider({ children, onOpenTerm }) {
  const [data, setData] = useState({ categories: [], terms: [] });
  useEffect(() => { fetchGlossary().then(setData).catch(() => {}); }, []);
  const value = useMemo(() => {
    const byId = new Map(data.terms.map((t) => [t.id, t]));
    // 3文字未満の別名(A/B など)は誤検出が多いので印を付けない。長い語から先に当てる
    const keys = data.terms.flatMap((t) => [t.term, ...t.aliases].filter((k) => k.length >= 3).map((k) => [k, t.id]))
      .sort((a, b) => b[0].length - a[0].length);
    const re = keys.length ? new RegExp(keys.map(([k]) => k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"), "g") : null;
    return { ...data, byId, re, keyToId: new Map(keys), open: onOpenTerm ?? (() => {}) };
  }, [data, onOpenTerm]);
  return <GlossaryCtx.Provider value={value}>{children}</GlossaryCtx.Provider>;
}

export const useGlossary = () => useContext(GlossaryCtx);

export function Term({ id, children }) {
  const { byId, open } = useGlossary();
  const [show, setShow] = useState(false);
  const t = byId.get(id);
  if (!t) return children;
  return (
    <span className="relative inline" onMouseEnter={() => setShow(true)} onMouseLeave={() => setShow(false)}>
      <span
        tabIndex={0}
        role="button"
        onFocus={() => setShow(true)}
        onBlur={() => setShow(false)}
        onClick={(e) => { e.stopPropagation(); setShow((s) => !s); }}
        className="cursor-help border-b border-dotted border-blue-400"
      >
        {children}
      </span>
      {show && (
        <span role="tooltip" className="absolute left-0 top-full z-50 mt-1 block w-72 rounded-lg border border-blue-500/60 bg-slate-900 p-2.5 text-left text-[12px] leading-relaxed text-slate-200 shadow-xl">
          <span className="block text-[11px] text-slate-500">{t.cat}</span>
          <span className="block text-[13px] font-semibold text-slate-100">{t.term}</span>
          <span className="mt-1 block font-semibold">{t.short}</span>
          <span className="mt-1 block text-slate-400">{t.detail}</span>
          <button onMouseDown={(e) => { e.preventDefault(); open(t.id); }} className="mt-1.5 text-xs text-blue-400 hover:underline">用語集で開く</button>
        </span>
      )}
    </span>
  );
}

/** 文字列中の既知の用語(その文字列の中での初出のみ)を <Term> で包む。初出の判定は描画ごとにやり直す */
export function TermText({ text }) {
  const { re, keyToId } = useGlossary();
  if (!re || !text) return text ?? null;
  const used = new Set();
  const out = [];
  let last = 0;
  for (const m of String(text).matchAll(re)) {
    const id = keyToId.get(m[0]);
    if (used.has(id)) continue;
    used.add(id);
    out.push(text.slice(last, m.index), <Term key={m.index} id={id}>{m[0]}</Term>);
    last = m.index + m[0].length;
  }
  out.push(String(text).slice(last));
  return <>{out}</>;
}

/** 用語集の一覧(分類チップと検索つき) */
export function GlossaryList({ focusId }) {
  const { categories, terms } = useGlossary();
  const [cat, setCat] = useState("");
  const [q, setQ] = useState("");
  useEffect(() => {
    if (!focusId) return;
    setCat(""); setQ("");
    requestAnimationFrame(() => document.getElementById(`gl-${focusId}`)?.scrollIntoView({ block: "center", behavior: "smooth" }));
  }, [focusId]);
  const ql = q.trim().toLowerCase();
  const list = terms.filter((t) => (!cat || t.cat === cat) && (!ql || [t.term, ...t.aliases, t.short, t.detail].join(" ").toLowerCase().includes(ql)));
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-1.5">
        {["", ...categories].map((c) => (
          <button key={c || "all"} onClick={() => setCat(c)}
            className={`rounded-full border px-2.5 py-0.5 text-[12px] ${cat === c ? "border-blue-500 bg-blue-500/15 text-blue-200" : "border-slate-700 text-slate-400 hover:text-slate-200"}`}>
            {c || `すべて(${terms.length})`}
          </button>
        ))}
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="用語を絞り込み" aria-label="用語を絞り込み"
          className="ml-auto w-48 rounded-md border border-slate-700 bg-slate-900 px-2 py-1 text-[13px]" />
      </div>
      <div className="grid gap-2.5 [grid-template-columns:repeat(auto-fill,minmax(260px,1fr))]">
        {list.map((t) => (
          <article key={t.id} id={`gl-${t.id}`}
            className={`rounded-xl border bg-slate-900 p-4 ${focusId === t.id ? "border-blue-500" : "border-slate-800"}`}>
            <div className="text-[11px] text-slate-500">{t.cat}</div>
            <h3 className="text-sm font-semibold text-slate-100">{t.term}</h3>
            {t.aliases.length > 0 && <div className="text-xs text-slate-500">別名: {t.aliases.join("、")}</div>}
            <p className="mt-1 text-[13px] font-semibold text-slate-200">{t.short}</p>
            <p className="mt-1 text-[13px] leading-relaxed text-slate-400">{t.detail}</p>
          </article>
        ))}
        {!list.length && <p className="text-[13px] text-slate-500">該当なし</p>}
      </div>
    </div>
  );
}

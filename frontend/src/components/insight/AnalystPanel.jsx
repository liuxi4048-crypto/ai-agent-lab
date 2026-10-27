import { useEffect, useRef, useState } from "react";
import { Bot, Loader2 } from "lucide-react";
import Markdown from "../md.jsx";
import { explainInsight } from "../../api.js";

/**
 * 分析エージェント: 記録から数えた事実をローカル LLM に渡し、解説と改善案を書かせる。
 * 生成文は推定(D)なので、実測の数値とは見た目で区別する。
 */
export default function AnalystPanel({ source, id, models }) {
  const [model, setModel] = useState("worker");
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState(null);
  const [error, setError] = useState(null);
  // 対象が変わったら実行中の分析の結果は捨てる(後から届いた古い解説を表示しない)
  const reqRef = useRef(0);
  useEffect(() => { reqRef.current += 1; setRes(null); setError(null); setBusy(false); }, [source, id]);
  const usable = (models?.models ?? []).filter((m) => m.installed && (!m.needs_ram || m.ram_ok));
  // 選択欄に出ているモデルと送るモデルを一致させる(worker が使えない環境では先頭の利用可能モデル)
  useEffect(() => {
    if (usable.length && !usable.some((m) => m.key === model)) setModel(usable[0].key);
  }, [usable, model]);

  const run = () => {
    const req = ++reqRef.current;
    setBusy(true); setError(null);
    explainInsight(source, id, model)
      .then((r) => { if (req === reqRef.current) setRes(r); })
      .catch((e) => { if (req === reqRef.current) setError(e.message); })
      .finally(() => { if (req === reqRef.current) setBusy(false); });
  };

  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <Bot size={16} className="text-violet-300" />
        <h3 className="text-sm font-semibold text-slate-100">分析エージェント(ローカルLLM)</h3>
        <span className="rounded-full bg-amber-500/90 px-2 py-0.5 text-xs font-bold text-slate-950" title="LLM が書いた推定。数値は記録から数えた事実だけを渡している">D 推定</span>
        <select value={model} onChange={(e) => setModel(e.target.value)} aria-label="分析に使うモデル"
          className="ml-auto rounded-md border border-slate-700 bg-slate-900 px-2 py-1 text-[13px]">
          {(usable.length ? usable : [{ key: "worker", tag: "worker" }]).map((m) => <option key={m.key} value={m.key}>{m.key}({m.tag})</option>)}
        </select>
        <button onClick={run} disabled={busy}
          className="flex items-center gap-1.5 rounded-lg bg-violet-600 px-3 py-1.5 text-[13px] font-semibold text-white hover:bg-violet-500 disabled:opacity-60">
          {busy ? <Loader2 size={14} className="animate-spin" /> : <Bot size={14} />} {busy ? "解説を作成中…" : "この記録を解説して"}
        </button>
      </div>
      <p className="mt-1.5 text-[12px] text-slate-500">記録の本文(プロンプトや出力)は渡さず、数えた事実(件数・時間・トークン・改善候補)だけを渡します。API 課金はありません。</p>
      {error && <p className="mt-2 text-[13px] text-red-400">{error}</p>}
      {res && (
        <div className="mt-2 border-t border-slate-800 pt-2">
          <div className="mb-1 text-xs text-slate-500">{res.model} ・ {res.seconds} 秒</div>
          <Markdown text={res.text} className="text-[13px]" />
          <details className="mt-2 text-[12px] text-slate-500">
            <summary className="cursor-pointer text-blue-400">渡した事実を見る</summary>
            <pre className="mt-1 max-h-60 overflow-auto rounded bg-slate-950 p-2 text-xs">{JSON.stringify(res.facts, null, 1)}</pre>
          </details>
        </div>
      )}
    </section>
  );
}

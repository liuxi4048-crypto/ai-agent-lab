import { Check } from "lucide-react";
import { PHASE_META, segmentByPhase } from "../derive.js";

export default function PhaseStepper({ nodes }) {
  const segments = segmentByPhase(nodes);
  return <nav aria-label="実行工程" className="mb-6 flex overflow-x-auto py-2">
    {Object.entries(PHASE_META).map(([phase, meta], index, all) => {
      const group = segments.filter(s => s.phase === phase).flatMap(s => s.nodes);
      const done = group.length > 0 && group.every(n => n.status === "done");
      const active = !done && group.some(n => ["thinking", "generating", "running"].includes(n.status));
      return <div key={phase} className="flex min-w-24 flex-1 items-start last:flex-none">
        <div className="flex flex-col items-center gap-1" title={meta.hint}>
          <span className={`flex h-7 w-7 items-center justify-center rounded-full border-2 ${done ? "border-blue-500 bg-blue-500 text-white" : active ? "border-blue-400 bg-blue-500/20 text-blue-400" : "border-slate-600 text-slate-500"}`}>{done ? <Check size={16} /> : active ? <span className="h-3 w-3 rounded-full bg-blue-400" /> : null}</span>
          <span className={`text-xs ${done || active ? "text-slate-100" : "text-slate-500"}`}>{meta.label}</span>
        </div>
        {index < all.length - 1 && <span className={`mx-2 mt-3 h-0.5 flex-1 ${done ? "bg-blue-500" : "bg-slate-700"}`} />}
      </div>;
    })}
  </nav>;
}

import { useEffect, useMemo, useState } from "react";
import { groupByBackend } from "../derive.js";

/**
 * モデル選択(カード式)。まず接続先サーバーのカード(Ollama / llama-server /
 * Strata / Claude API など、サーバーごとに分かれている)を選び、そのサーバーの
 * モデルをチップから選ぶ。モックアップ v1/v3 の見せ方に合わせたもの。
 *
 * describe(m) は呼び出し側が各モデルの表示情報を返す:
 *   { blocked: 選べないか, note: " ※未導入" 等の注記, suffix: "(用途)" 等の補足 }
 * extraOptions は auto / none のような「モデル以外」の選択肢で、チップで出す。
 */
export default function ModelPicker({ models, value, onChange, extraOptions = [], describe }) {
  const groups = useMemo(() => groupByBackend(models), [models]);
  const valueGroupKey = groups.find((g) => g.models.some((m) => m.key === value))?.key ?? null;
  const [activeKey, setActiveKey] = useState(valueGroupKey ?? groups[0]?.key ?? null);

  // 外から選択が変わったら(プリフィル等)、そのモデルのサーバーを開いておく
  useEffect(() => {
    if (valueGroupKey) setActiveKey(valueGroupKey);
  }, [valueGroupKey]);

  const active = groups.find((g) => g.key === activeKey) ?? groups[0] ?? null;

  const groupNote = (g) => {
    const sts = g.models.map((m) => m.backend_status).filter(Boolean);
    if (sts.length && sts.every((s) => s === "server_unreachable")) return "未接続";
    if (sts.some((s) => s === "no_api_key")) return "APIキー未設定";
    return `${g.models.length} モデル`;
  };

  const chipCls = (selected) =>
    `flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-[13px] transition-colors ${
      selected
        ? "border-blue-500 bg-blue-500/15 text-slate-100"
        : "border-slate-700 bg-slate-950 text-slate-300 hover:border-slate-500"
    }`;

  return (
    <div>
      {extraOptions.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-2">
          {extraOptions.map((o) => (
            <button
              key={o.value}
              type="button"
              title={o.title}
              onClick={() => onChange(o.value)}
              aria-pressed={value === o.value}
              className={chipCls(value === o.value)}
            >
              {o.label}
            </button>
          ))}
        </div>
      )}

      {/* サーバー選択カード */}
      <div className="flex flex-wrap gap-2" role="group" aria-label="接続先サーバー">
        {groups.map((g) => {
          const contains = g.models.some((m) => m.key === value);
          const isActive = active?.key === g.key;
          return (
            <button
              key={g.key}
              type="button"
              onClick={() => setActiveKey(g.key)}
              aria-pressed={isActive}
              className={`min-w-40 rounded-xl border px-3 py-2 text-left transition-colors ${
                isActive
                  ? "border-blue-500 bg-blue-500/10"
                  : "border-slate-700 bg-slate-950 hover:border-slate-500"
              }`}
            >
              <span className="flex items-center gap-1.5 text-[13px] font-semibold text-slate-100">
                <span className="min-w-0 flex-1 truncate">{g.label}</span>
                {contains && <span className="h-2 w-2 shrink-0 rounded-full bg-blue-400" title="選択中のモデルはこのサーバー" />}
              </span>
              <span className="mt-0.5 block text-[11px] text-slate-500">{groupNote(g)}</span>
            </button>
          );
        })}
      </div>

      {/* 選択中サーバーのモデル */}
      {active && (
        <div className="mt-2 flex flex-wrap gap-2" role="group" aria-label="モデル">
          {active.models.map((m) => {
            const d = describe ? describe(m) : { blocked: false, note: "", suffix: "" };
            const selected = value === m.key;
            return (
              <button
                key={m.key}
                type="button"
                disabled={d.blocked}
                onClick={() => onChange(m.key)}
                aria-pressed={selected}
                title={d.blocked ? (d.note || "").trim() || "このモデルは今使えません" : m.tag}
                className={`${chipCls(selected)} disabled:cursor-not-allowed disabled:opacity-50`}
              >
                <span className="font-medium">{m.tag}</span>
                {d.suffix && <span className="text-xs text-slate-400">{d.suffix}</span>}
                {d.note && <span className="text-[11px] text-amber-400">{d.note.trim()}</span>}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

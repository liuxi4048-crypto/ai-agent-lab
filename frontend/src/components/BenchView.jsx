import { useEffect, useMemo, useState } from "react";
import { Gauge, Play, Loader2, X, ChevronRight, Info, ArrowUpDown } from "lucide-react";
import { fetchBenchSuite } from "../api.js";
import { fmtAgo, fmtNum as fmt, fmtMs } from "../derive.js";

// 選定状態(models.yaml の tier)の短い説明。保留/退役モデルの再ベンチがこの画面の用途の1つ
const TIER_LABEL = {
  agent: null, critic: "批評専用", probation: "保留(要再ベンチ)", external: "外部ツール用", archive: "退役",
};

/**
 * ベンチの設定ビュー(右ペイン全面)+ 過去ベンチのモデル比較表。
 *
 * 「選んだ1モデルに固定プロンプト集を投げる」ので、入力はモデル・課題・反復・judge だけ。
 * 実行すると mode="bench" の Run になり、結果は通常の Run 画面(BenchPanel)で見る。
 * prefill: [設定変更して再実行] からの流し込み {model, tasks, repeats, judge}
 */
export default function BenchView({ models, runs, onStart, onCancel, prefill, onSelectRun }) {
  const [suite, setSuite] = useState(null);
  const [suiteError, setSuiteError] = useState(null);
  const [model, setModel] = useState("");
  const [selected, setSelected] = useState(null);   // null=全課題(suite読込後に確定)
  const [repeats, setRepeats] = useState(1);
  const [judge, setJudge] = useState("auto");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [latestOnly, setLatestOnly] = useState(true);
  const [sortKey, setSortKey] = useState("created_at");

  useEffect(() => {
    fetchBenchSuite().then(setSuite).catch((e) => setSuiteError(e.message));
  }, []);

  const modelOptions = models?.models ?? [];
  const usable = (m) => m.installed && (!m.needs_ram || m.ram_ok);

  // 既定モデル: prefill → 導入済みの先頭。prefill のモデルが今は使えない
  // (未導入化・RAM不足)なら既定へ落とし、表示と送信値が食い違わないようにする
  const firstUsable = () => modelOptions.find(usable)?.key ?? modelOptions[0]?.key ?? "";
  const usableKey = (key) => modelOptions.some((m) => m.key === key && usable(m));
  useEffect(() => {
    if (!modelOptions.length) return;
    if (prefill?.model) {
      setModel(usableKey(prefill.model) ? prefill.model : firstUsable());
      setSelected(prefill.tasks?.length ? new Set(prefill.tasks) : null);
      setRepeats(prefill.repeats ?? 1);
      const j = prefill.judge ?? "auto";
      setJudge(j === "auto" || j === "none" || usableKey(j) ? j : "auto");
      return;
    }
    if (!model) setModel(firstUsable());
  }, [prefill, modelOptions]);   // eslint-disable-line react-hooks/exhaustive-deps

  const tasks = suite?.tasks ?? [];
  const isSelected = (id) => selected === null || selected.has(id);
  const selectedIds = tasks.filter((t) => isSelected(t.id)).map((t) => t.id);
  const toggleTask = (id) => {
    setSelected((cur) => {
      const next = new Set(cur === null ? tasks.map((t) => t.id) : cur);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const canSubmit = usableKey(model) && selectedIds.length > 0 && !!suite;
  const submit = async () => {
    if (!canSubmit || busy) return;
    setBusy(true);
    setError(null);
    try {
      // 全課題選択なら空配列(サーバー側で全課題)。並び順は suite 定義に従う
      await onStart({
        model, repeats, judge,
        tasks: selectedIds.length === tasks.length ? [] : selectedIds,
      });
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  // ---- 比較表: 完了したベンチRunの集計(速度軸 / 品質軸) ----
  const rows = useMemo(() => {
    const done = runs.filter((r) => r.mode === "bench" && r.status === "done" && r.bench?.aggregate);
    const seen = new Set();
    const list = [];
    for (const r of [...done].sort((a, b) => b.created_at - a.created_at)) {
      if (latestOnly) {
        if (seen.has(r.model)) continue;
        seen.add(r.model);
      }
      list.push(r);
    }
    const val = (r) => {
      const a = r.bench.aggregate;
      return { created_at: r.created_at, tok_per_s: a.tok_per_s, warm_ttft_ms: a.warm_ttft_ms,
               quality_score: a.quality_score }[sortKey] ?? -Infinity;
    };
    const asc = sortKey === "warm_ttft_ms";
    return list.sort((a, b) => (asc ? val(a) - val(b) : val(b) - val(a)));
  }, [runs, latestOnly, sortKey]);

  const th = (label, key, title) => (
    <th
      key={key}
      onClick={() => key && setSortKey(key)}
      title={title}
      className={`px-2 py-1.5 text-left font-semibold ${key ? "cursor-pointer select-none hover:text-zinc-200" : ""} ${sortKey === key ? "text-blue-300" : ""}`}
    >
      <span className="inline-flex items-center gap-1">
        {label}{key && <ArrowUpDown size={10} className="opacity-50" />}
      </span>
    </th>
  );

  return (
    <div className="flex h-full flex-col items-center overflow-y-auto px-4 py-8">
      <div className="w-full max-w-3xl">
        <div className="mb-3 flex items-center gap-2">
          <Gauge size={15} className="text-blue-400" />
          <h2 className="text-sm font-semibold text-zinc-100">ローカルLLM性能ベンチ</h2>
          <span className="hidden text-[11px] text-zinc-600 sm:inline">
            固定の課題を1モデルに投げ、速度と成果物の質を測る
          </span>
          {onCancel && (
            <button
              onClick={onCancel}
              className="ml-auto flex items-center gap-1 rounded-md px-2 py-1 text-[11.5px] text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200"
              title="ベンチ設定をやめて元の画面へ戻る(Esc)"
            >
              <X size={13} /> キャンセル
            </button>
          )}
        </div>

        {/* 設定 */}
        <div className="rounded-xl border border-zinc-800 bg-zinc-900 p-4">
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-2 text-xs text-zinc-400">
              対象モデル
              <select
                value={model}
                onChange={(e) => setModel(e.target.value)}
                className="max-w-80 rounded-md border border-zinc-700 bg-zinc-950 px-2 py-2 text-xs text-zinc-200 outline-none focus:border-blue-500"
              >
                {modelOptions.map((m) => {
                  const tier = TIER_LABEL[m.tier];
                  const note = !m.installed ? " ※未導入"
                    : m.needs_ram && !m.ram_ok ? " ※RAM不足"
                    : m.needs_ram ? " ※RAM併用" : "";
                  return (
                    <option key={m.key} value={m.key} disabled={!usable(m)}>
                      {m.tag}{m.for ? ` (${m.for})` : ""}{tier ? ` [${tier}]` : ""}{note}
                    </option>
                  );
                })}
              </select>
            </label>
            <label className="flex items-center gap-2 text-xs text-zinc-400" title="LLM採点に使うモデル。auto=対象と異なるファミリーを自動で選ぶ(自己評価の甘さを避ける)">
              judge
              <select
                value={judge}
                onChange={(e) => setJudge(e.target.value)}
                className="rounded-md border border-zinc-700 bg-zinc-950 px-2 py-2 text-xs text-zinc-200 outline-none focus:border-blue-500"
              >
                <option value="auto">auto(異ファミリー)</option>
                <option value="none">なし(自動チェックのみ)</option>
                {modelOptions.filter(usable).map((m) => (
                  <option key={m.key} value={m.key}>{m.tag}</option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-2 text-xs text-zinc-400" title="各課題の反復回数。2以上にすると速度のばらつき(±)が出る">
              反復
              <input
                type="number" min={1} max={5} value={repeats}
                onChange={(e) => setRepeats(Math.min(5, Math.max(1, Number(e.target.value) || 1)))}
                className="w-14 rounded border border-zinc-700 bg-zinc-950 px-1.5 py-1.5 text-zinc-200 outline-none focus:border-blue-500"
              />
            </label>
            <button
              onClick={submit}
              disabled={busy || !canSubmit}
              className="ml-auto flex items-center gap-1.5 rounded-md bg-green-600 px-5 py-2 text-sm font-semibold text-white transition-colors hover:bg-green-500 disabled:cursor-not-allowed disabled:bg-zinc-700 disabled:text-zinc-500"
            >
              {busy ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
              ベンチ開始
            </button>
          </div>

          {/* 課題 */}
          <div className="mt-3 flex items-center gap-2 text-[11px] text-zinc-500">
            <span className="font-semibold uppercase tracking-wider">課題</span>
            <span className="tabular-nums">{selectedIds.length}/{tasks.length}</span>
            <button onClick={() => setSelected(null)} className="underline-offset-2 hover:text-zinc-300 hover:underline">全て</button>
            <button onClick={() => setSelected(new Set())} className="underline-offset-2 hover:text-zinc-300 hover:underline">解除</button>
            {suite?.version != null && <span className="ml-auto">suite v{suite.version}</span>}
          </div>
          {suiteError && <p className="mt-1 text-xs text-red-400">{suiteError}</p>}
          <ul className="mt-1.5 grid grid-cols-1 gap-1.5 sm:grid-cols-2">
            {tasks.map((t) => (
              <li key={t.id}>
                <details className="group rounded-lg border border-zinc-800 bg-zinc-950/60">
                  <summary className="flex cursor-pointer select-none items-center gap-2 px-2.5 py-2 text-xs text-zinc-300">
                    <input
                      type="checkbox"
                      checked={isSelected(t.id)}
                      onChange={() => toggleTask(t.id)}
                      onClick={(e) => e.stopPropagation()}
                      className="accent-blue-500"
                    />
                    <span className="min-w-0 flex-1 truncate">{t.title}</span>
                    <span className="shrink-0 rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-400">{t.category}</span>
                    <ChevronRight size={12} className="shrink-0 text-zinc-600 transition-transform group-open:rotate-90" />
                  </summary>
                  <pre className="max-h-48 overflow-auto whitespace-pre-wrap border-t border-zinc-800 px-2.5 py-2 text-[11px] leading-relaxed text-zinc-400">
                    {t.prompt}
                  </pre>
                  <p className="border-t border-zinc-800 px-2.5 py-1 text-[10.5px] text-zinc-600">
                    自動チェック: {t.checks.length ? t.checks.join(", ") : "なし(採点のみ)"}
                  </p>
                </details>
              </li>
            ))}
          </ul>
          {error && <p className="mt-2 text-xs text-red-400">{error}</p>}
        </div>

        {/* 比較表 */}
        <div className="mt-6">
          <div className="mb-1.5 flex items-center gap-2">
            <p className="text-[10.5px] font-semibold uppercase tracking-wider text-zinc-600">モデル比較(完了したベンチ)</p>
            <label className="ml-auto flex cursor-pointer items-center gap-1 text-[11px] text-zinc-500">
              <input type="checkbox" checked={latestOnly} onChange={(e) => setLatestOnly(e.target.checked)} className="accent-blue-500" />
              モデルごとに最新のみ
            </label>
          </div>
          {rows.length === 0 ? (
            <p className="rounded-lg border border-dashed border-zinc-800 px-3 py-4 text-center text-[12px] text-zinc-600">
              まだ完了したベンチがありません
            </p>
          ) : (
            <div className="overflow-x-auto rounded-lg border border-zinc-800">
              <table className="w-full text-[11.5px] text-zinc-300">
                <thead className="bg-zinc-900 text-[10.5px] uppercase tracking-wider text-zinc-500">
                  <tr>
                    {th("モデル", null)}
                    {th("課題", null, "課題数×反復(比較は同じ条件のRun同士で)")}
                    {th("tok/s", "tok_per_s", "生成速度(thinking含む)。反復2以上なら±標準偏差")}
                    {th("TTFT", "warm_ttft_ms", "初トークンまでの時間(ウォーム=モデルロードを含まない反復の平均)")}
                    {th("ロード", null, "最大モデルロード時間(コールド)")}
                    {th("チェック", null, "決定論チェックの合格数")}
                    {th("採点", null, "LLM採点の平均(1〜5)")}
                    {th("品質", "quality_score", "チェック合格率と採点の平均(0〜100)")}
                    {th("日時", "created_at")}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => {
                    const a = r.bench.aggregate;
                    return (
                      <tr
                        key={r.id}
                        onClick={() => onSelectRun(r.id)}
                        className="cursor-pointer border-t border-zinc-800 hover:bg-zinc-800/60"
                        title="クリックでこのベンチの詳細を開く"
                      >
                        <td className="px-2 py-1.5 font-semibold text-zinc-100">{r.model_tag || r.model}</td>
                        <td className="px-2 py-1.5 tabular-nums text-zinc-500">
                          {r.bench.tasks?.length ?? "?"}{r.bench.repeats > 1 ? `×${r.bench.repeats}` : ""}
                        </td>
                        <td className="px-2 py-1.5 tabular-nums font-semibold text-blue-300">
                          {fmt(a.tok_per_s)}{a.tok_per_s_sd != null && <span className="text-zinc-500"> ±{fmt(a.tok_per_s_sd)}</span>}
                        </td>
                        <td className="px-2 py-1.5 tabular-nums">{fmtMs(a.warm_ttft_ms ?? a.ttft_ms)}</td>
                        <td className="px-2 py-1.5 tabular-nums text-zinc-500">{fmtMs(a.max_load_ms)}</td>
                        <td className="px-2 py-1.5 tabular-nums">{a.checks_passed}/{a.checks_total}</td>
                        <td className="px-2 py-1.5 tabular-nums">{a.judge_avg != null ? `${fmt(a.judge_avg, 2)}/5` : "—"}</td>
                        <td className="px-2 py-1.5 tabular-nums font-semibold text-green-300">{fmt(a.quality_score, 0)}</td>
                        <td className="px-2 py-1.5 tabular-nums text-zinc-500">{fmtAgo(r.created_at)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          <p className="mt-2 flex items-start gap-1.5 text-[10.5px] leading-relaxed text-zinc-600">
            <Info size={11} className="mt-0.5 shrink-0" />
            単発生成のベンチです。tok/s が高くてもエージェント実運用(ツールループ)の完走を保証しません
            (2026-08-20実測: 9.7 tok/s のモデルが 21.5 tok/s のモデルより先に完走)。
            LLM採点は judge モデルの癖を含むため、同じ judge で測った Run 同士で比べてください。
          </p>
        </div>
      </div>
    </div>
  );
}

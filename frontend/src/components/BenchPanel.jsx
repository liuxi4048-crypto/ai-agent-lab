import { Gauge, Award, ExternalLink, Info, Loader2, AlertTriangle } from "lucide-react";
import { fmtNum as fmt, fmtMs } from "../derive.js";

const PHASE_LABEL = {
  generate: "生成中", judge: "採点中", done: "完了", cancelled: "中断", error: "エラー",
};

function Stat({ label, value, sub, accent }) {
  return (
    <div className="min-w-0">
      <p className="text-[10px] uppercase tracking-wider text-zinc-500">{label}</p>
      <p className={`truncate text-lg font-bold tabular-nums leading-tight ${accent ?? "text-zinc-100"}`}>{value}</p>
      {sub && <p className="truncate text-[10.5px] text-zinc-500">{sub}</p>}
    </div>
  );
}

/**
 * ベンチRunの結果パネル(Run画面の最上部)。
 *
 * 2軸を並べる: 左=速度性能(tok/s・TTFT・ロード)、右=成果物の質(自動チェック・LLM採点)。
 * データは Run サマリーの bench(GET /runs の3秒ポーリング + 課題ごとの中間保存)から読む。
 * 途中経過は行ごとに埋まっていき、未計測は「—」のまま。
 */
export default function BenchPanel({ bench, status, othersRunning = 0 }) {
  if (!bench) return null;
  const a = bench.aggregate ?? {};
  const live = status === "running" || status === "queued";
  const phase = PHASE_LABEL[bench.phase] ?? bench.phase;
  const judgeScores = (s) => (s ? `${s.requirements}/${s.correctness}/${s.quality}` : null);

  return (
    <section className="border-b border-zinc-800 bg-zinc-900/60 px-4 py-3">
      <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-zinc-500">
        <span className="font-semibold text-zinc-300">{bench.model_tag}</span>
        <span>judge: {bench.judge_tag ?? "なし"}</span>
        <span>反復 {bench.repeats}</span>
        <span>num_ctx {bench.num_ctx}</span>
        {bench.think != null && <span>think={String(bench.think)}</span>}
        <span className={`ml-auto flex items-center gap-1 ${live ? "text-blue-300" : ""}`}>
          {live && <Loader2 size={11} className="animate-spin" />}
          {phase}{live && a.reps_total ? ` ${a.reps_done ?? 0}/${a.reps_total}` : ""}
        </span>
      </div>

      {live && othersRunning > 0 && (
        <p className="mb-2 flex items-center gap-1.5 rounded-md border border-yellow-500/40 bg-yellow-500/10 px-2 py-1 text-[11px] text-yellow-300">
          <AlertTriangle size={12} className="shrink-0" />
          他のRunが{othersRunning}件同時に実行中です。GPU/モデルを取り合うため速度の計測値は汚れます
        </p>
      )}
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <div className="rounded-xl border border-blue-500/30 bg-blue-500/5 p-3">
          <div className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold text-blue-300">
            <Gauge size={12} /> 速度性能(LLM自体の動き)
          </div>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="生成速度" value={`${fmt(a.tok_per_s)} tok/s`} accent="text-blue-300"
                  sub={a.tok_per_s_sd != null ? `±${fmt(a.tok_per_s_sd)} (反復間)` : "thinking含む"} />
            <Stat label="初トークン(TTFT)" value={fmtMs(a.warm_ttft_ms ?? a.ttft_ms)}
                  sub={a.warm_ttft_ms != null ? `全体平均 ${fmtMs(a.ttft_ms)}` : "ロード込み"} />
            <Stat label="最大ロード" value={fmtMs(a.max_load_ms)} sub="コールド起動" />
            <Stat label="生成トークン" value={a.eval_tokens ?? "—"} sub={a.gen_wall_s != null ? `生成合計 ${a.gen_wall_s}s` : null} />
          </div>
        </div>
        <div className="rounded-xl border border-green-500/30 bg-green-500/5 p-3">
          <div className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold text-green-300">
            <Award size={12} /> 成果物の質
          </div>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="品質スコア" value={a.quality_score != null ? `${fmt(a.quality_score, 0)}` : "—"} accent="text-green-300"
                  sub="チェック合格率と採点の平均" />
            <Stat label="自動チェック" value={a.checks_total ? `${a.checks_passed}/${a.checks_total}` : "—"}
                  sub={a.check_pass_rate != null ? `合格率 ${Math.round(a.check_pass_rate * 100)}%` : "決定論(LLM不使用)"} />
            <Stat label="LLM採点" value={a.judge_avg != null ? `${fmt(a.judge_avg, 2)}/5` : "—"}
                  sub="要件・正確性・完成度" />
            <Stat label="問題" value={`${(a.truncated ?? 0) + (a.errors ?? 0)}`}
                  sub={`打ち切り ${a.truncated ?? 0} / エラー ${a.errors ?? 0}`}
                  accent={(a.truncated ?? 0) + (a.errors ?? 0) > 0 ? "text-yellow-300" : undefined} />
          </div>
        </div>
      </div>

      <div className="mt-3 overflow-x-auto rounded-lg border border-zinc-800">
        <table className="w-full text-[11.5px] text-zinc-300">
          <thead className="bg-zinc-900 text-[10.5px] uppercase tracking-wider text-zinc-500">
            <tr>
              <th className="px-2 py-1.5 text-left font-semibold">課題</th>
              <th className="px-2 py-1.5 text-left font-semibold">tok/s</th>
              <th className="px-2 py-1.5 text-left font-semibold" title="初トークンまで(ロード込み)">TTFT</th>
              <th className="px-2 py-1.5 text-left font-semibold">ロード</th>
              <th className="px-2 py-1.5 text-left font-semibold">tokens</th>
              <th className="px-2 py-1.5 text-left font-semibold">チェック</th>
              <th className="px-2 py-1.5 text-left font-semibold" title="要件/正確性/完成度(各1〜5)">採点</th>
              <th className="px-2 py-1.5 text-left font-semibold">成果物</th>
            </tr>
          </thead>
          <tbody>
            {bench.tasks.map((t) => {
              const s = t.summary ?? {};
              const reps = t.reps ?? [];
              const failed = reps.filter((r) => r.error).length;
              const checkClass = s.checks_total
                ? s.checks_passed === s.checks_total ? "text-green-300" : s.checks_passed === 0 ? "text-red-300" : "text-yellow-300"
                : "text-zinc-500";
              return (
                <tr key={t.id} className="border-t border-zinc-800">
                  <td className="px-2 py-1.5">
                    <span className="text-zinc-100">{t.title}</span>
                    <span className="ml-1.5 rounded bg-zinc-800 px-1 py-0.5 text-[10px] text-zinc-400">{t.category}</span>
                    {s.truncated > 0 && <span className="ml-1.5 text-[10px] text-yellow-400" title="長さ上限で打ち切り">⚠打切</span>}
                    {s.timing_missing > 0 && <span className="ml-1.5 text-[10px] text-yellow-400" title="Ollama が done チャンクを返さず速度を計測できなかった反復あり">⚠速度不明</span>}
                    {s.suspicious && <span className="ml-1.5 text-[10px] text-orange-400" title="自動チェックが落ちているのに採点が高い: judge が成果物内の文言に釣られた疑い">⚠採点乖離</span>}
                    {failed > 0 && <span className="ml-1.5 text-[10px] text-red-400">エラー</span>}
                  </td>
                  <td className="px-2 py-1.5 tabular-nums font-semibold text-blue-300">{fmt(s.tok_per_s)}</td>
                  <td className="px-2 py-1.5 tabular-nums">{fmtMs(s.ttft_ms)}</td>
                  <td className="px-2 py-1.5 tabular-nums text-zinc-500">{fmtMs(s.load_ms)}</td>
                  <td className="px-2 py-1.5 tabular-nums text-zinc-500">{s.eval_tokens != null ? Math.round(s.eval_tokens) : "—"}</td>
                  <td className={`px-2 py-1.5 tabular-nums ${checkClass}`}>
                    {s.checks_total ? `${s.checks_passed}/${s.checks_total}` : "—"}
                  </td>
                  <td className="px-2 py-1.5 tabular-nums" title={reps.map((r) => r.judge?.comment).filter(Boolean).join("\n\n")}>
                    {s.judge_avg != null ? (
                      <>
                        <span className="font-semibold text-green-300">{fmt(s.judge_avg)}</span>
                        {s.judge_scores && (
                          <span className="ml-1 text-zinc-500">
                            ({judgeScores({
                              requirements: fmt(s.judge_scores.requirements, 0),
                              correctness: fmt(s.judge_scores.correctness, 0),
                              quality: fmt(s.judge_scores.quality, 0),
                            })})
                          </span>
                        )}
                      </>
                    ) : "—"}
                  </td>
                  <td className="px-2 py-1.5">
                    <span className="flex flex-wrap gap-1.5">
                      {reps.map((r) => r.artifact && (
                        <a
                          key={r.rep}
                          href={r.artifact.path}
                          target="_blank"
                          rel="noreferrer"
                          className="inline-flex items-center gap-0.5 text-blue-400 underline-offset-2 hover:underline"
                          title={`${r.artifact.name} (${r.artifact.chars}文字)`}
                        >
                          {r.artifact.name}{reps.length > 1 ? ` #${r.rep}` : ""}<ExternalLink size={10} />
                        </a>
                      ))}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="mt-2 flex items-start gap-1.5 text-[10.5px] leading-relaxed text-zinc-600">
        <Info size={11} className="mt-0.5 shrink-0" />
        採点コメントは各「品質評価」カードと採点セルのツールチップにあります。
        単発生成の計測であり、エージェント実運用の完走性能は別指標です。
      </p>
    </section>
  );
}

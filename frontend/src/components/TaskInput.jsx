import { useEffect, useMemo, useRef, useState } from "react";
import { Play, Loader2, X, Sparkles, ChevronRight, History } from "lucide-react";

// 進め方(mode)と成果物形式(deliverable)はメインエージェントが自動で決めるため、
// UIには枠を置かない。ユーザーはタスクとモデルだけ決めればよい。

// 詳細設定の既定値。「既定と異なる件数」の算出と[クリア]の戻し先に使う
const DEFAULTS = { model: "auto", critique: false, approve: true, maxIter: 18, allowRam: false, claudeReview: false };

/**
 * 新しいタスクの Composer(下書きビュー = 右ペイン全面)。
 *
 * 旧「上部に挿入されるバー型パネル」は、入力パネルと旧Runの詳細が同時に立って
 * Master-Detail の同期を破る不満1の直接原因だったため廃止。下書き中は右ペインが
 * これ単独になる。App 側で常時マウント+hidden 切替されるため、Run を見に行って
 * 戻っても書きかけは消えない。
 * prefill: [設定変更して再実行] からの流し込み {task, mode, model, ...}
 * onCancel: 下書きをやめて直前のRunへ戻る(戻り先が無ければ App が null を渡す)
 * suggestions: 空欄時に出す開始チップ [{emoji?, text}](直近タスク+サンプル)
 */
export default function TaskInput({ models, onStart, prefill, onCancel, visible, suggestions = [] }) {
  const [task, setTask] = useState("");
  const taskRef = useRef(null);
  const [model, setModel] = useState(DEFAULTS.model);
  const [critique, setCritique] = useState(DEFAULTS.critique);
  const [approve, setApprove] = useState(DEFAULTS.approve);
  const [maxIter, setMaxIter] = useState(DEFAULTS.maxIter);
  const [allowRam, setAllowRam] = useState(DEFAULTS.allowRam);   // 既定はVRAMのみ
  const [claudeReview, setClaudeReview] = useState(DEFAULTS.claudeReview);  // 既定OFF(外部送信のため)
  const [inherited, setInherited] = useState(false);  // 設定変更して再実行から来たか
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  // src は Run レコード(snake_case)にも DEFAULTS(camelCase)にも対応する
  const applySettings = (src) => {
    setModel(src.model ?? DEFAULTS.model);
    setCritique(!!src.critique);
    setApprove(src.approve ?? DEFAULTS.approve);
    setMaxIter(src.max_iter ?? src.maxIter ?? DEFAULTS.maxIter);
    setAllowRam(!!(src.allow_ram ?? src.allowRam));
    setClaudeReview(!!(src.claude_review ?? src.claudeReview));
  };

  useEffect(() => {
    if (!prefill) return;
    // task キーの無い空prefill = 「設定だけ既定へ戻す」。書きかけの本文は消さない
    // (Runを見に行って戻っても下書きが残る、という保持の約束を守る)
    if (prefill.task !== undefined) setTask(prefill.task);
    applySettings(prefill);
    // _inherit 付きprefill=「設定変更して再実行」由来。なぜフォームが埋まっているかを
    // チップで説明し、[クリア]で既定へ戻せるようにする(黙って引き継がない)
    setInherited(!!prefill._inherit);
  }, [prefill]);

  // パネルが開いたら入力欄へフォーカスを移す(Nキーだけで書き始められるように)
  useEffect(() => {
    if (visible) taskRef.current?.focus({ preventScroll: true });
  }, [visible]);

  const submit = async () => {
    if (!task.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      // mode / deliverable は送らない(auto = メインエージェントが決める)
      await onStart({ task: task.trim(), model, critique, approve,
                      max_iter: maxIter, allow_ram: allowRam,
                      claude_review: claudeReview });
      setTask("");   // 実行を開始したら入力を空に戻す(ビュー切替は App 側)
      setInherited(false);
    } catch (e) {
      setError(e.message);   // 失敗時は下書きに留まる(入力・設定は保持)
    } finally {
      setBusy(false);
    }
  };

  const modelOptions = models?.models ?? [];
  const claude = models?.claude ?? null;

  // APIキーが無い等で使えなくなったら、選択が残らないようOFFへ戻す
  useEffect(() => {
    if (claude && !claude.available && claudeReview) setClaudeReview(false);
  }, [claude, claudeReview]);

  // 軽量hybrid(RAMオフロード8GB以下)は実測で実用速度のため、トグルOFFでも選択可
  // (router.pick_model の自動選択と同じ基準。coder=qwen3:30b が該当)
  const isLightHybrid = (m) => m.needs_ram && (m.ram_gb ?? 0) <= 8;

  // RAM併用をOFFに戻したとき、選択中の大型モデルが残らないよう auto へ落とす
  useEffect(() => {
    const cur = modelOptions.find((m) => m.key === model);
    if (cur?.needs_ram && !isLightHybrid(cur) && (!allowRam || !cur.ram_ok)) setModel("auto");
    if (cur && isLightHybrid(cur) && !cur.ram_ok) setModel("auto");
  }, [allowRam, model, modelOptions]);

  // 詳細設定のうち既定値から外れている件数(折りたたみ先の状態を隠さない)
  const diffCount = useMemo(() => [
    critique !== DEFAULTS.critique,
    approve !== DEFAULTS.approve,
    allowRam !== DEFAULTS.allowRam,
    claudeReview !== DEFAULTS.claudeReview,
    maxIter !== DEFAULTS.maxIter,
  ].filter(Boolean).length, [critique, approve, allowRam, claudeReview, maxIter]);

  return (
    <div className="flex h-full flex-col items-center overflow-y-auto px-4 py-8">
      <div className="w-full max-w-2xl">
        <div className="mb-3 flex items-center gap-2">
          <Sparkles size={15} className="text-blue-400" />
          <h2 className="text-sm font-semibold text-zinc-100">新しいタスク</h2>
          <span className="hidden text-[11px] text-zinc-600 sm:inline">
            進め方・成果物の形式はエージェントが自動で決めます
          </span>
          {onCancel && (
            <button
              onClick={onCancel}
              className="ml-auto flex items-center gap-1 rounded-md px-2 py-1 text-[11.5px] text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200"
              title="下書きをやめて元の画面へ戻る(Esc)"
            >
              <X size={13} /> キャンセル
            </button>
          )}
        </div>

        {inherited && (
          <div className="mb-2 flex items-center gap-2 rounded-lg border border-blue-500/40 bg-blue-500/10 px-3 py-1.5 text-[11.5px] text-blue-300">
            <History size={12} className="shrink-0" />
            <span className="min-w-0 flex-1">元のタスクから設定を引き継ぎました</span>
            <button
              onClick={() => {
                applySettings(DEFAULTS);
                setInherited(false);
              }}
              className="shrink-0 rounded px-1.5 py-0.5 text-blue-300 underline-offset-2 hover:underline"
            >
              クリア
            </button>
          </div>
        )}

        <textarea
          ref={taskRef}
          value={task}
          onChange={(e) => setTask(e.target.value)}
          onKeyDown={(e) => {
            // IME変換中は確定のためのEnterなので送信しない
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !e.nativeEvent.isComposing) submit();
          }}
          rows={6}
          placeholder="何を作りますか? / 何を調べますか? (Ctrl+Enterで実行)"
          className="w-full resize-none rounded-xl border border-zinc-700 bg-zinc-950 px-4 py-3 text-sm leading-relaxed text-zinc-100 placeholder-zinc-600 outline-none focus:border-blue-500"
        />

        <div className="mt-2 flex flex-wrap items-center gap-2">
          <select
            value={model}
            onChange={(e) => setModel(e.target.value)}
            title="使うモデル。auto ならタスク内容に合わせて自動で選ばれる"
            className="max-w-72 rounded-md border border-zinc-700 bg-zinc-950 px-2 py-2 text-xs text-zinc-200 outline-none focus:border-blue-500"
          >
            <option value="auto">auto (タスクに合わせて自動選択)</option>
            {modelOptions.map((m) => {
              // RAM併用モデルは、トグルOFF・空きRAM不足なら選ばせない。
              // ただし軽量hybrid(≤8GB)は実用速度なのでトグル不要(自動選択と同じ基準)
              const light = isLightHybrid(m);
              const blocked = !m.installed
                || (m.needs_ram && !m.ram_ok)
                || (m.needs_ram && !light && !allowRam);
              const note = !m.installed ? " ※未導入"
                : m.needs_ram && !m.ram_ok ? " ※RAM不足"
                : m.needs_ram && !light && !allowRam ? " ※RAM併用をONに"
                : m.needs_ram && !light ? " ※低速" : "";
              return (
                <option key={m.key} value={m.key} disabled={blocked}>
                  {m.tag}{m.for ? ` (${m.for})` : ""}{note}
                </option>
              );
            })}
          </select>

          {/* 安全・コストに響く設定は折りたたみ中でも常時チップで見せる */}
          {!approve && (
            <span className="rounded-full border border-red-500/60 bg-red-500/10 px-2 py-0.5 text-[10.5px] font-semibold text-red-300">
              承認なしで実行
            </span>
          )}
          {claudeReview && (
            <span className="rounded-full border border-orange-500/60 bg-orange-500/10 px-2 py-0.5 text-[10.5px] font-semibold text-orange-300">
              🤖 Claudeレビュー
            </span>
          )}

          <button
            onClick={submit}
            disabled={busy || !task.trim()}
            className="ml-auto flex items-center gap-1.5 rounded-md bg-green-600 px-5 py-2 text-sm font-semibold text-white transition-colors hover:bg-green-500 disabled:cursor-not-allowed disabled:bg-zinc-700 disabled:text-zinc-500"
          >
            {busy ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
            実行
          </button>
        </div>

        <details className="group mt-3 rounded-lg border border-zinc-800">
          <summary className="flex cursor-pointer select-none items-center gap-1.5 px-3 py-2 text-[11.5px] text-zinc-400 hover:text-zinc-200">
            <ChevronRight size={12} className="transition-transform group-open:rotate-90" />
            詳細設定
            {diffCount > 0 && (
              <span className="rounded-full bg-blue-500/20 px-1.5 py-0.5 text-[10px] font-semibold tabular-nums text-blue-300">
                {diffCount}件が既定と異なります
              </span>
            )}
          </summary>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-zinc-800 px-3 py-2.5 text-xs text-zinc-400">
            <label className="flex cursor-pointer items-center gap-1.5">
              <input type="checkbox" checked={approve} onChange={(e) => setApprove(e.target.checked)}
                     className="accent-blue-500" />
              コマンド実行前に承認
            </label>
            <label className="flex cursor-pointer items-center gap-1.5">
              <input type="checkbox" checked={critique} onChange={(e) => setCritique(e.target.checked)}
                     className="accent-blue-500" />
              完了後レビュー(critique)
            </label>
            <label
              className="flex cursor-pointer items-center gap-1.5"
              title="ONにするとVRAMに収まらない大型モデル(RAM併用)も使えます。品質は上がりますが低速になります"
            >
              <input type="checkbox" checked={allowRam} onChange={(e) => setAllowRam(e.target.checked)}
                     className="accent-blue-500" />
              大きいモデルも使う(RAM併用・低速)
              {models?.free_ram_gb != null && (
                <span className="text-zinc-600">空き{models.free_ram_gb}GB</span>
              )}
            </label>
            <label
              className={`flex items-center gap-1.5 ${claude?.available ? "cursor-pointer" : "cursor-not-allowed opacity-50"}`}
              title={claude?.available
                ? `完了後にClaude(${claude.model})が成果物をレビューし、その場で修正して最終成果物に仕上げます。`
                  + "\n※ローカルのClaude Code CLIをサブスク認証で使います。API課金は発生しませんが、"
                  + "サブスクの5時間利用枠を消費します"
                : (claude?.reason || "Claudeレビューは利用できません")}
            >
              <input type="checkbox" checked={claudeReview} disabled={!claude?.available}
                     onChange={(e) => setClaudeReview(e.target.checked)}
                     className="accent-orange-500" />
              <span className={claudeReview ? "text-orange-400" : ""}>
                🤖 Claudeが最終レビュー
              </span>
              <span className="text-zinc-600">※サブスク枠を消費</span>
            </label>
            <label className="flex items-center gap-1.5">
              最大イテレーション
              <input
                type="number" min={1} max={60} value={maxIter}
                onChange={(e) => setMaxIter(Number(e.target.value) || DEFAULTS.maxIter)}
                className="w-14 rounded border border-zinc-700 bg-zinc-950 px-1.5 py-0.5 text-zinc-200 outline-none focus:border-blue-500"
              />
            </label>
          </div>
        </details>

        {error && <p className="mt-2 text-xs text-red-400">{error}</p>}

        {suggestions.length > 0 && !task.trim() && (
          <div className="mt-5">
            <p className="mb-1.5 text-[10.5px] font-semibold uppercase tracking-wider text-zinc-600">
              ここから始める
            </p>
            <div className="flex flex-wrap gap-2">
              {suggestions.map((s) => (
                <button
                  key={s.text}
                  onClick={() => {
                    setTask(s.text);
                    taskRef.current?.focus({ preventScroll: true });
                  }}
                  className="max-w-full truncate rounded-full border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 hover:border-blue-500 hover:text-blue-400"
                  title={s.text}
                >
                  {s.emoji ? `${s.emoji} ` : ""}{s.text}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

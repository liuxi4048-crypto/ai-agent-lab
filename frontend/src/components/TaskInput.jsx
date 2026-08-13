import { useEffect, useRef, useState } from "react";
import { Play, Loader2, X, Sparkles } from "lucide-react";

// 進め方(mode)と成果物形式(deliverable)はメインエージェントが自動で決めるため、
// UIには枠を置かない。ユーザーはタスクとモデルだけ決めればよい。

/**
 * タスク入力パネル。開閉は App が管理する(サイドバーの「+ 新しいタスク」/ Nキー)。
 * 実行を開始すると App 側が閉じる。入力途中の内容は hidden で隠されるだけなので失われない。
 * prefill: [設定変更して再実行] からの流し込み {task, mode, model, ...}
 * onClose: パネルを閉じる(未指定なら閉じるボタンを出さない)
 */
export default function TaskInput({ models, onStart, running, prefill, onClose, visible }) {
  const [task, setTask] = useState("");
  const taskRef = useRef(null);
  const [model, setModel] = useState("auto");
  const [critique, setCritique] = useState(false);
  const [approve, setApprove] = useState(true);
  const [maxIter, setMaxIter] = useState(18);
  const [allowRam, setAllowRam] = useState(false);   // 既定はVRAMのみ
  const [claudeReview, setClaudeReview] = useState(false);  // 既定OFF(外部送信のため)
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!prefill) return;
    setTask(prefill.task ?? "");
    setModel(prefill.model ?? "auto");
    setCritique(!!prefill.critique);
    setApprove(prefill.approve ?? true);
    setMaxIter(prefill.max_iter ?? 18);
    setAllowRam(!!prefill.allow_ram);
    setClaudeReview(!!prefill.claude_review);
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
      setTask("");   // 実行を開始したら入力を空に戻す(パネルは App 側が閉じる)
    } catch (e) {
      setError(e.message);
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

  return (
    <div className="border-b border-zinc-800 bg-zinc-900/60 px-4 py-3">
      <div className="mb-2 flex items-center gap-2">
        <Sparkles size={13} className="text-blue-400" />
        <h2 className="text-[11px] font-semibold uppercase tracking-wider text-zinc-400">
          新しいタスク
        </h2>
        <span className="text-[10px] text-zinc-600">
          進め方・成果物の形式はエージェントが自動で決めます
        </span>
        {onClose && (
          <button
            onClick={onClose}
            className="ml-auto rounded-md p-1 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200"
            title="閉じる(Esc)"
          >
            <X size={14} />
          </button>
        )}
      </div>
      <div className="flex flex-wrap items-start gap-2">
        <textarea
          ref={taskRef}
          value={task}
          onChange={(e) => setTask(e.target.value)}
          onKeyDown={(e) => {
            // IME変換中は確定のためのEnterなので送信しない
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !e.nativeEvent.isComposing) submit();
          }}
          rows={2}
          placeholder="全体タスクを入力 (Ctrl+Enterで実行)…"
          className="min-w-60 flex-1 resize-none rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-600 outline-none focus:border-blue-500"
        />
        <select
          value={model}
          onChange={(e) => setModel(e.target.value)}
          title="使うモデル。auto ならタスク内容に合わせて自動で選ばれる"
          className="max-w-80 rounded-md border border-zinc-700 bg-zinc-950 px-2 py-2 text-xs text-zinc-200 outline-none focus:border-blue-500"
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
        <button
          onClick={submit}
          disabled={busy || !task.trim()}
          className="flex items-center gap-1.5 rounded-md bg-green-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-green-500 disabled:cursor-not-allowed disabled:bg-zinc-700 disabled:text-zinc-500"
        >
          {busy ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
          実行
        </button>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-4 text-xs text-zinc-400">
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
            onChange={(e) => setMaxIter(Number(e.target.value) || 18)}
            className="w-14 rounded border border-zinc-700 bg-zinc-950 px-1.5 py-0.5 text-zinc-200 outline-none focus:border-blue-500"
          />
        </label>
      </div>
      {error && <p className="mt-2 text-xs text-red-400">{error}</p>}
    </div>
  );
}

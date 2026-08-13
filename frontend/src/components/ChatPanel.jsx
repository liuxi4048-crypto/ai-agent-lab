import { useEffect, useRef, useState } from "react";
import { MessageSquare, Send, Loader2, User, Bot, Play } from "lucide-react";
import { continueRun } from "../api.js";
import Markdown from "./md.jsx";

const TEXTAREA_MAX_PX = 128; // max-h-32 (8rem) と揃える

/**
 * 成果物に会話形式で修正を加えるパネル(ワークスペース下部に常設)。
 *
 * 実行ツリーのノードから会話スレッドを組み立てて表示する:
 *   task ノード          → 最初の依頼(ユーザー)
 *   追加指示の coder     → 追加の依頼(ユーザー)
 *   answer ノード        → その回の結果(エージェント)
 * 送信すると同じRun・同じワークスペースの続きとして実行される。
 */
export default function ChatPanel({ runId, nodes, order, running, resumable, runStatus, onSent }) {
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [expanded, setExpanded] = useState(false);
  const [pendingMsg, setPendingMsg] = useState(null);
  // 送信直後〜SSEで実行開始が届くまでの隙間で二重送信されるのを防ぐ(409になる)
  const [sentAt, setSentAt] = useState(null);
  const endRef = useRef(null);
  const textareaRef = useRef(null);
  const prevThreadLenRef = useRef(0);

  const thread = buildThread(nodes, order);

  // サーバー側にノードが増えたら楽観バブルをクリアする
  useEffect(() => {
    if (thread.length > prevThreadLenRef.current) {
      setPendingMsg(null);
    }
    prevThreadLenRef.current = thread.length;
  }, [thread.length]);

  useEffect(() => {
    if (!expanded) return;
    endRef.current?.scrollIntoView({ block: "nearest" });
  }, [thread.length, running, expanded, pendingMsg]);

  // 入力内容に応じてtextareaの高さを自動調整(max-h-32まで)
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, TEXTAREA_MAX_PX)}px`;
  }, [msg]);

  // 実行開始が届いた(またはRunを切り替えた)ら送信ロックを解除する。
  // SSEが遅れても詰まらないよう20秒で自動解除する。
  // ※ フックは早期returnより前に全部並べる(runIdの有無でフック数が変わると
  //   React error #310「Rendered more hooks than during the previous render」になる)
  useEffect(() => {
    if (!sentAt) return;
    if (running) { setSentAt(null); return; }
    const t = setTimeout(() => setSentAt(null), 20_000);
    return () => clearTimeout(t);
  }, [sentAt, running]);
  // Runを切り替えたら送信ロック・楽観バブル・エラーを捨てる(別Runの会話に混ざる)
  useEffect(() => { setSentAt(null); setPendingMsg(null); setError(null); }, [runId]);

  if (!runId) return null;

  const send = async () => {
    const text = msg.trim();
    if (!text || busy || running || sentAt) return;
    setBusy(true);
    setError(null);
    setPendingMsg(text);
    setSentAt(Date.now());
    try {
      await continueRun(runId, text);
      setMsg("");
      onSent?.();
    } catch (e) {
      setError(e.message);
      setPendingMsg(null); // 送信失敗時は楽観バブルを消す
      setSentAt(null);
    } finally {
      setBusy(false);
    }
  };

  // 実行中でも下書きは書けるようにする(送信は send() 側で弾く)
  const canType = resumable || running;
  const sendDisabled = busy || running || !resumable || !!sentAt;
  const placeholder = running
    ? "実行中 — 完了後に送信できます(下書きは保持されます)"
    : "成果物への追加指示(例: 難易度を上げて / 色を変えて / スコア表示を追加して)";

  return (
    <section className="border-t border-zinc-800 bg-zinc-950/80">
      <div className="flex items-center gap-2 px-4 pt-3 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
        <MessageSquare size={12} /> 会話で修正する
        {(thread.length > 0 || pendingMsg) && (
          <button
            onClick={() => setExpanded((v) => !v)}
            className="ml-auto flex items-center gap-1 normal-case tracking-normal text-zinc-400 hover:text-zinc-200"
          >
            履歴 {thread.length}件 {expanded ? "▴" : "▾"}
          </button>
        )}
      </div>

      {expanded && (thread.length > 0 || pendingMsg) && (
        <div className="max-h-56 space-y-2 overflow-y-auto px-4 py-2">
          {thread.map((m, i) => (
            <div key={i} className="flex gap-2 text-[12.5px]">
              <span
                className={`mt-0.5 shrink-0 ${m.role === "user" ? "text-blue-400" : "text-green-400"}`}
                title={m.role === "user" ? "あなた" : "エージェント"}
              >
                {m.role === "user" ? <User size={13} /> : <Bot size={13} />}
              </span>
              {m.role === "user" ? (
                <p className="min-w-0 flex-1 whitespace-pre-wrap break-words rounded-lg bg-blue-500/10 px-2.5 py-1.5 text-zinc-200">
                  {m.text}
                </p>
              ) : (
                <Markdown text={m.text} className="min-w-0 flex-1 rounded-lg bg-zinc-900 px-2.5 py-1.5 text-zinc-300" />
              )}
            </div>
          ))}
          {pendingMsg && (
            <div className="flex gap-2 text-[12.5px] opacity-50">
              <span className="mt-0.5 shrink-0 text-blue-400" title="あなた">
                <User size={13} />
              </span>
              <p className="min-w-0 flex-1 whitespace-pre-wrap break-words rounded-lg bg-blue-500/10 px-2.5 py-1.5 italic text-zinc-300">
                {pendingMsg} <span className="not-italic text-zinc-500">(送信中…)</span>
              </p>
            </div>
          )}
          <div ref={endRef} />
        </div>
      )}

      <div className="flex items-end gap-2 px-4 pb-3 pt-1">
        <textarea
          ref={textareaRef}
          value={msg}
          onChange={(e) => setMsg(e.target.value)}
          onKeyDown={(e) => {
            // IME変換確定のEnterで誤送信しないよう isComposing を確認する
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              send();
            }
          }}
          rows={1}
          disabled={!canType}
          placeholder={placeholder}
          className="max-h-32 min-h-[38px] flex-1 resize-none overflow-y-auto rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder-zinc-600 outline-none focus:border-blue-500 disabled:opacity-50"
        />
        <button
          onClick={send}
          disabled={sendDisabled || !msg.trim()}
          title="Enterで送信 / Shift+Enterで改行"
          className="flex h-[38px] items-center gap-1.5 rounded-md bg-blue-600 px-4 text-sm font-semibold text-white hover:bg-blue-500 disabled:cursor-not-allowed disabled:bg-zinc-700 disabled:text-zinc-500"
        >
          {busy ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />}
          送信
        </button>
      </div>

      {error && <p className="px-4 pb-2 text-xs text-red-400">{error}</p>}
      {!resumable && !running && (
        <p className="flex items-center gap-1.5 px-4 pb-3 text-[11px] text-zinc-600">
          <Play size={10} />
          {runStatus === "queued"
            ? "待機中 — 開始後に追加指示を送れます"
            : runStatus === "interrupted" || runStatus === "error"
              ? "中断・失敗したRunです。追加指示を送ると続きから修正できます"
              : "このRunは継続できません(完了後に再度お試しください)"}
        </p>
      )}
    </section>
  );
}

/** ノード木 → 会話スレッド。
 *
 * 追加指示ノードの判定は title の接頭辞「🛠 追加指示」を第一に見るが、それだけに
 * 頼らない。過去に保存されたRunでは毎iterのタイトル更新で接頭辞が失われている
 * ことがあるため、構造(taskの直下の coder で、直前に answer が出ている=次の
 * ユーザーターン)からも拾う。detail には送信した指示文が残っている。
 */
function buildThread(nodes, order) {
  const out = [];
  const ids = order ?? [];
  const taskId = ids.find((id) => nodes?.[id]?.kind === "task");
  let seenAnswer = false;      // 1回でも回答が出たか(=次のcoderは追加指示の可能性)
  let userTurnTaken = true;    // 直近の回答に対するユーザーターンを既に拾ったか
  for (const id of ids) {
    const n = nodes?.[id];
    if (!n) continue;
    if (n.kind === "task") {
      out.push({ role: "user", text: n.detail || n.title });
      continue;
    }
    if (n.kind === "coder" && n.detail) {
      const isFollowUp = n.title?.startsWith("🛠 追加指示")
        || (n.parent_id === taskId && seenAnswer && !userTurnTaken);
      if (isFollowUp) {
        out.push({ role: "user", text: n.detail });
        userTurnTaken = true;
      }
      continue;
    }
    if (n.kind === "answer" && n.output) {
      out.push({ role: "agent", text: n.output });
      seenAnswer = true;
      userTurnTaken = false;
    }
  }
  return out;
}

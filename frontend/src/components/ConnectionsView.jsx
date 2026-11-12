import { useCallback, useEffect, useState } from "react";
import { Plug, Plus, Copy, Check, RefreshCw, Trash2, Play, Square } from "lucide-react";
import {
  fetchConnections, addConnection, fetchConnectionCode, rotateConnection, deleteConnection,
  startGateway, stopGateway, putGateway,
} from "../api.js";
import { fmtAgo } from "../derive.js";

/**
 * 外部連携ビュー(右ペイン全面): 他のアプリ向けの接続の保存・接続コードのコピー・LLM窓口の起動停止。
 * 合言葉は一覧では返ってこない。コードは [接続コードをコピー] か追加直後にだけ取得する。
 */
export default function ConnectionsView({ models }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);   // コピー済みなどの短い確認
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [model, setModel] = useState("");
  const [created, setCreated] = useState(null); // 追加直後 {name, code}
  const [busy, setBusy] = useState(false);

  // 窓口が配信するのは models.yaml のローカル(ollama)モデルだけ。自動検出分(scanned)は含まれない
  const options = (models?.models ?? []).filter((m) => !m.scanned && (m.backend ?? "ollama") === "ollama");

  const reload = useCallback(
    () => fetchConnections().then((d) => { setData(d); setError(null); }).catch((e) => setError(e.message)),
    [],
  );
  useEffect(() => { reload(); }, [reload]);
  useEffect(() => {
    if (!model && options.length) setModel(options.find((m) => m.tag === "gemma3:12b")?.tag ?? options[0].tag);
  }, [options, model]);

  const run = async (fn) => {
    setBusy(true);
    try { await fn(); setError(null); } catch (e) { setError(e.message); }
    setBusy(false);
  };
  const flash = (text) => {
    setNotice(text);
    setTimeout(() => setNotice(null), 2500);
  };
  const copy = async (code) => {
    await navigator.clipboard.writeText(code);
    flash("コピーしました");
  };
  const gatewayAction = (fn) => run(async () => {
    await fn();
    await new Promise((r) => setTimeout(r, 1500));   // 起動直後は待ち受け開始まで少しかかる
    await reload();
  });

  const gw = data?.gateway;
  const btn = "flex items-center gap-1 rounded-md border border-zinc-700 px-2 py-1 text-[12px] text-zinc-300 hover:border-zinc-500 hover:text-zinc-100 disabled:opacity-50";

  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-5">
      <div className="mx-auto max-w-3xl space-y-5">
        <h2 className="flex items-center gap-2 text-[15px] font-semibold text-zinc-100">
          <Plug size={16} /> 外部連携
        </h2>
        {error && <p className="rounded-md border border-red-500/50 px-3 py-2 text-[12px] text-red-300">{error}</p>}

        {gw && (
          <div className="flex flex-wrap items-center gap-3 rounded-lg border border-zinc-800 px-3 py-2.5 text-[13px] text-zinc-300">
            <span className={`h-2 w-2 rounded-full ${gw.running ? "bg-emerald-400" : "bg-zinc-600"}`} />
            <span>LLM窓口: {gw.running ? "起動中" : "停止中"}</span>
            <span className="font-mono text-zinc-400">{gw.host}:{gw.port}</span>
            {gw.running
              ? <button className={btn} disabled={busy} onClick={() => gatewayAction(stopGateway)}><Square size={12} /> 停止</button>
              : <button className={btn} disabled={busy} onClick={() => gatewayAction(startGateway)}><Play size={12} /> 起動</button>}
            <label className="ml-auto flex items-center gap-1.5 text-[12px] text-zinc-400">
              <input
                type="checkbox"
                checked={gw.autostart}
                onChange={(e) => run(async () => { setData({ ...data, gateway: await putGateway({ autostart: e.target.checked }) }); })}
              />
              起動時に自動で開始
            </label>
          </div>
        )}

        <div className="space-y-2">
          {data?.connections.map((c) => (
            <div key={c.id} className="flex flex-wrap items-center gap-3 rounded-lg border border-zinc-800 px-3 py-2.5">
              <div className="min-w-0 flex-1">
                <div className="truncate text-[13px] font-semibold text-zinc-100">{c.name}</div>
                <div className="text-[12px] text-zinc-500">
                  {c.model} ・ {c.last_used_at ? `最終利用 ${fmtAgo(c.last_used_at)}（${c.last_client ?? "?"}）` : "未使用"}
                </div>
              </div>
              <button className={btn} disabled={busy} onClick={() => run(async () => copy((await fetchConnectionCode(c.id)).code))}>
                <Copy size={12} /> 接続コードをコピー
              </button>
              <button
                className={btn}
                disabled={busy}
                onClick={() => {
                  if (!confirm(`「${c.name}」の合言葉を再発行します。今の接続コードは使えなくなります。`)) return;
                  run(async () => { await rotateConnection(c.id); flash("再発行しました。新しいコードをコピーしてください"); });
                }}
              >
                <RefreshCw size={12} /> 合言葉を再発行
              </button>
              <button
                className={`${btn} hover:!border-red-500 hover:!text-red-300`}
                disabled={busy}
                onClick={() => {
                  if (!confirm(`「${c.name}」を削除します。この接続コードは使えなくなります。`)) return;
                  run(async () => { await deleteConnection(c.id); await reload(); });
                }}
              >
                <Trash2 size={12} /> 削除
              </button>
            </div>
          ))}
          {data && !data.connections.length && <p className="text-[12px] text-zinc-500">連携はまだありません</p>}
        </div>

        {adding ? (
          <div className="flex flex-wrap gap-2 rounded-lg border border-zinc-800 p-3">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="連携の名前（例: 結婚式アプリ）"
              className="min-w-0 flex-1 rounded-md border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-[13px] text-zinc-200 outline-none focus:border-blue-500"
            />
            <select
              value={model}
              onChange={(e) => setModel(e.target.value)}
              className="rounded-md border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-[13px] text-zinc-200"
            >
              {options.map((m) => <option key={m.key} value={m.tag}>{m.tag}</option>)}
            </select>
            <button
              className={btn}
              disabled={busy || !name.trim() || !model}
              onClick={() => run(async () => {
                const r = await addConnection(name.trim(), model);
                setCreated({ name: r.name, code: r.code });
                setName("");
                setAdding(false);
                await reload();
              })}
            >
              追加
            </button>
            <button className={btn} onClick={() => setAdding(false)}>キャンセル</button>
          </div>
        ) : (
          <button className={btn} onClick={() => { setAdding(true); setCreated(null); }}><Plus size={12} /> 連携を追加</button>
        )}

        {created && (
          <div className="space-y-2 rounded-lg border border-emerald-600/40 bg-emerald-500/5 p-3">
            <p className="text-[13px] text-zinc-200">「{created.name}」を追加しました。接続コード:</p>
            <div className="flex items-center gap-2">
              <code className="min-w-0 flex-1 break-all rounded bg-zinc-950 px-2 py-1.5 text-[12px] text-zinc-300">{created.code}</code>
              <button className={btn} onClick={() => copy(created.code).catch((e) => setError(e.message))}><Copy size={12} /> コピー</button>
            </div>
            <p className="text-[12px] text-zinc-400">
              相手側では <code>npm run demo:ai -- --connect &lt;接続コード&gt;</code>
            </p>
          </div>
        )}

        {notice && (
          <p className="flex items-center gap-1 text-[12px] text-emerald-400"><Check size={12} /> {notice}</p>
        )}
      </div>
    </div>
  );
}

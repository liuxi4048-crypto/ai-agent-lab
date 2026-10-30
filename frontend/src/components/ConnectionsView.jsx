import { useEffect, useState } from "react";
import { Check, Copy, KeyRound, RefreshCw } from "lucide-react";
import { fetchApiKeyStatus, generateApiKey } from "../api.js";
import { modelStatusNote, serverDisplayName, shortBaseUrl } from "../derive.js";

// 接続状態パネル: バックエンド/サーバーごとに「今つながっているか・
// 何が使えるか」を1画面で確認する。変更できるのは OpenAI互換APIのキー発行だけ
// (モデル登録は models.yaml、Claude APIキーは環境変数が正)。

const CONN_TITLE = {
  ollama: "Ollama（このPC）",
  anthropic: "Claude API（クラウド・従量課金）",
};

function connTitle(c) {
  if (c.kind === "openai") return serverDisplayName(c.name, c.base_url);
  return CONN_TITLE[c.kind] ?? c.kind;
}

function statusChip(c) {
  if (c.kind === "anthropic")
    return c.key_set
      ? { cls: "border-green-500/50 bg-green-500/10 text-green-300", text: "APIキー設定済み" }
      : { cls: "border-red-500/50 bg-red-500/10 text-red-300", text: "APIキー未設定" };
  return c.reachable
    ? { cls: "border-green-500/50 bg-green-500/10 text-green-300", text: "接続OK" }
    : { cls: "border-red-500/50 bg-red-500/10 text-red-300", text: "未接続" };
}

function registeredFor(c, models) {
  if (c.kind === "openai")
    return models.filter((m) => m.backend === "openai" && (m.base_url || "") === c.base_url);
  if (c.kind === "anthropic") return models.filter((m) => m.backend === "anthropic");
  return models.filter((m) => (m.backend ?? "ollama") === "ollama");
}

const KEY_SOURCE = {
  env: { cls: "border-green-500/50 bg-green-500/10 text-green-300", text: "環境変数で設定済み" },
  file: { cls: "border-green-500/50 bg-green-500/10 text-green-300", text: "発行済み" },
  none: { cls: "border-amber-500/50 bg-amber-500/10 text-amber-300", text: "未発行（認証なし）" },
};

// OpenAI互換API(/v1)のキー発行カード。生成したキーは発行直後の1回だけ表示する。
function ApiKeyCard() {
  const [status, setStatus] = useState(null);
  const [newKey, setNewKey] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    fetchApiKeyStatus().then(setStatus, (e) => setError(e.message));
  }, []);

  const handleGenerate = async () => {
    if (status?.source === "file" &&
        !window.confirm("今のキーは使えなくなります。新しいキーを発行しますか？")) return;
    setBusy(true);
    setError("");
    try {
      const { key } = await generateApiKey();
      setNewKey(key);
      setCopied(false);
      setStatus((s) => ({ ...s, source: "file" }));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const handleCopy = () =>
    navigator.clipboard.writeText(newKey).then(() => setCopied(true), () => setError("コピーできませんでした"));

  const chip = status && KEY_SOURCE[status.source];
  const endpoint = `http://${status?.host === "0.0.0.0" ? "<このPCのIP>" : status?.host ?? "127.0.0.1"}:${status?.port ?? 8765}/v1`;

  return (
    <section className="mt-4 rounded-xl border border-slate-800 bg-slate-900 p-4">
      <div className="flex items-center gap-2">
        <KeyRound size={15} className="shrink-0 text-slate-400" />
        <h3 className="min-w-0 flex-1 truncate text-sm font-semibold text-slate-100">OpenAI互換API のキー</h3>
        {chip && (
          <span className={`shrink-0 rounded-full border px-2 py-0.5 text-xs font-semibold ${chip.cls}`}>{chip.text}</span>
        )}
      </div>
      <p className="mt-1 font-mono text-xs text-slate-500">接続先 {endpoint}</p>
      <p className="mt-2 text-[13px] leading-relaxed text-slate-400">
        外部ツールから呼ぶときは <span className="font-mono">Authorization: Bearer &lt;キー&gt;</span> を付けます。
        キーは api_settings.json に保存され、再起動なしで有効になります。
      </p>

      {status && status.source !== "env" && (
        <button
          onClick={handleGenerate}
          disabled={busy}
          className="mt-3 rounded-lg border border-slate-700 px-3 py-2 text-[13px] text-slate-200 transition-colors hover:border-slate-500 hover:text-white disabled:opacity-50"
        >
          {status.source === "file" ? "キーを再発行" : "キーを発行"}
        </button>
      )}
      {status?.source === "env" && (
        <p className="mt-3 text-xs text-slate-500">環境変数 AAL_API_KEY が優先されるため、画面からは発行できません。</p>
      )}

      {newKey && (
        <div className="mt-3 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3">
          <p className="text-xs font-semibold text-amber-300">このキーは今だけ表示されます。控えてください。</p>
          <div className="mt-2 flex items-center gap-2">
            <code className="min-w-0 flex-1 break-all font-mono text-[13px] text-slate-100">{newKey}</code>
            <button
              onClick={handleCopy}
              className="flex shrink-0 items-center gap-1 rounded-md border border-slate-700 px-2 py-1 text-xs text-slate-200 hover:border-slate-500"
            >
              {copied ? <Check size={13} /> : <Copy size={13} />} {copied ? "コピー済み" : "コピー"}
            </button>
          </div>
        </div>
      )}
      {error && <p className="mt-2 text-xs text-red-400">{error}</p>}
    </section>
  );
}

export default function ConnectionsView({ models, onRefresh }) {
  const [refreshing, setRefreshing] = useState(false);
  const catalog = models;
  const list = catalog?.models ?? [];
  const conns = catalog?.connections ?? [];

  const handleRefresh = () => {
    setRefreshing(true);
    Promise.resolve(onRefresh?.()).finally(() => setTimeout(() => setRefreshing(false), 400));
  };

  return (
    <main className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto max-w-5xl p-4 sm:p-6">
        <div className="flex flex-wrap items-start gap-3">
          <div className="min-w-0 flex-1">
            <h2 className="text-2xl font-bold text-slate-100">接続状態</h2>
            <p className="mt-1 text-[13px] leading-relaxed text-slate-400">
              バックエンドごとに、今つながっているか・どのモデルが使えるかをまとめて確認できます。
              モデルを選ぶことが、そのまま接続先の切り替えになります。
            </p>
          </div>
          <button
            onClick={handleRefresh}
            className="flex items-center gap-1.5 rounded-lg border border-slate-700 px-3 py-2 text-[13px] text-slate-200 transition-colors hover:border-slate-500 hover:text-white"
          >
            <RefreshCw size={14} className={refreshing ? "animate-spin" : ""} /> 再確認
          </button>
        </div>

        {!catalog ? (
          <p className="mt-8 text-sm text-slate-500">読み込み中…</p>
        ) : (
          <div className="mt-5 grid gap-4 md:grid-cols-2">
            {conns.map((c) => {
              const chip = statusChip(c);
              const registered = registeredFor(c, list);
              return (
                <section
                  key={`${c.kind}:${c.base_url}`}
                  className="rounded-xl border border-slate-800 bg-slate-900 p-4"
                >
                  <div className="flex items-center gap-2">
                    <h3 className="min-w-0 flex-1 truncate text-sm font-semibold text-slate-100">
                      {connTitle(c)}
                    </h3>
                    <span className={`shrink-0 rounded-full border px-2 py-0.5 text-xs font-semibold ${chip.cls}`}>
                      {chip.text}
                    </span>
                  </div>
                  <p className="mt-1 font-mono text-xs text-slate-500">接続先 {shortBaseUrl(c.base_url)}</p>

                  {c.kind !== "anthropic" && (
                    <div className="mt-3">
                      <p className="text-xs font-semibold text-slate-400">
                        {c.kind === "ollama" ? "導入済みモデル" : "サーバーが公開しているモデル"}
                      </p>
                      {c.reachable ? (
                        c.served.length > 0 ? (
                          <div className="mt-1.5 flex flex-wrap gap-1.5">
                            {c.served.map((id) => (
                              <span
                                key={id}
                                className="rounded-full border border-slate-700 bg-slate-800/60 px-2 py-0.5 font-mono text-xs text-slate-300"
                              >
                                {id}
                              </span>
                            ))}
                          </div>
                        ) : (
                          <p className="mt-1 text-xs text-slate-500">（なし）</p>
                        )
                      ) : (
                        <p className="mt-1 text-xs text-slate-500">
                          {c.kind === "ollama"
                            ? "Ollamaが起動していません"
                            : "サーバーが起動すると、ここに公開モデルが表示されます"}
                        </p>
                      )}
                    </div>
                  )}

                  <div className="mt-3 border-t border-slate-800 pt-3">
                    <p className="text-xs font-semibold text-slate-400">この接続先を使うモデル（models.yaml登録）</p>
                    {registered.length > 0 ? (
                      <ul className="mt-1.5 space-y-1">
                        {registered.map((m) => (
                          <li key={m.key} className="flex items-baseline gap-2 text-[13px]">
                            <span className="font-medium text-slate-200">{m.tag}</span>
                            <span className="text-slate-500">{m.key}</span>
                            {m.installed ? (
                              <span className="ml-auto shrink-0 font-semibold text-green-400">使用可</span>
                            ) : (
                              <span className="ml-auto shrink-0 font-semibold text-red-400">
                                {modelStatusNote(m).replace(/^ ※/, "") || "使用不可"}
                              </span>
                            )}
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p className="mt-1 text-xs text-slate-500">未登録です</p>
                    )}
                  </div>
                </section>
              );
            })}
          </div>
        )}

        <ApiKeyCard />

        <p className="mt-6 text-xs leading-relaxed text-slate-500">
          APIキーの発行以外の設定はこの画面では行いません。モデルの登録・接続先・表示名（server_name）は models.yaml、
          Claude API のキーは環境変数 ANTHROPIC_API_KEY で設定します（キーを画面に表示・保存することはありません）。
        </p>
      </div>
    </main>
  );
}

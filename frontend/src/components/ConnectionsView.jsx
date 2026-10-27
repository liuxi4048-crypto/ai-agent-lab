import { useState } from "react";
import { RefreshCw } from "lucide-react";
import { modelStatusNote } from "../derive.js";

// 接続状態パネル: バックエンド/サーバーごとに「今つながっているか・
// 何が使えるか」を1画面で確認する。設定の変更はここでは行わない
// (モデル登録は models.yaml、APIキーは環境変数が正)。

const CONN_TITLE = {
  ollama: "Ollama（このPC）",
  anthropic: "Claude API（クラウド・従量課金）",
};

function connTitle(c) {
  if (c.kind === "openai") return c.name || c.base_url;
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
                  <p className="mt-1 break-all font-mono text-xs text-slate-500">{c.base_url}</p>

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

        <p className="mt-6 text-xs leading-relaxed text-slate-500">
          設定の変更はこの画面では行いません。モデルの登録・接続先・表示名（server_name）は models.yaml、
          Claude API のキーは環境変数 ANTHROPIC_API_KEY で設定します（キーを画面に表示・保存することはありません）。
        </p>
      </div>
    </main>
  );
}

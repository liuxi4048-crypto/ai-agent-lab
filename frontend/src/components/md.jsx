import { useMemo } from "react";

// 依存追加なしの最小限Markdown整形コンポーネント(react-markdown等は導入しない方針)。
// 対応するのは以下のみ:
//   ```コードブロック```  /  `インラインコード`  /  **強調**  /  行頭「- 」リスト  /  「## 」見出し
// それ以外は改行を保った通常テキストとして表示する。汎用MDパーサではないので過度な期待はしないこと。

// `code` と **bold** をインライン要素へ変換する(入れ子は考慮しない簡易実装)。
function renderInline(text, keyPrefix) {
  const nodes = [];
  let n = 0;
  for (const codeSeg of text.split(/(`[^`]+`)/g)) {
    if (codeSeg.startsWith("`") && codeSeg.endsWith("`") && codeSeg.length > 1) {
      nodes.push(
        <code key={`${keyPrefix}-c${n++}`} className="rounded bg-zinc-800 px-1 py-0.5 font-mono text-[0.9em] text-blue-300">
          {codeSeg.slice(1, -1)}
        </code>,
      );
      continue;
    }
    for (const boldSeg of codeSeg.split(/(\*\*[^*]+\*\*)/g)) {
      if (boldSeg.startsWith("**") && boldSeg.endsWith("**") && boldSeg.length > 3) {
        nodes.push(
          <b key={`${keyPrefix}-b${n++}`} className="font-semibold text-zinc-100">
            {boldSeg.slice(2, -2)}
          </b>,
        );
      } else if (boldSeg) {
        nodes.push(boldSeg);
      }
    }
  }
  return nodes;
}

/**
 * text をブロック要素の配列へ変換する。
 * ``````で分割してコードブロックを抜き出し、残りを行単位で見出し/リスト/段落へ振り分ける。
 */
function renderBlocks(text) {
  const blocks = [];
  const fenceParts = text.split("```");

  fenceParts.forEach((chunk, idx) => {
    if (idx % 2 === 1) {
      // 奇数インデックス = コードブロック本体。先頭行が言語指定のみならそれを取り除く。
      const body = chunk.replace(/^[ \t]*[\w-]*\n/, "").replace(/\n$/, "");
      blocks.push(
        <pre key={`code-${idx}`} className="my-1.5 overflow-x-auto rounded-lg border border-zinc-800 bg-black/40 p-2.5 font-mono text-[12px] leading-relaxed text-zinc-200">
          <code>{body}</code>
        </pre>,
      );
      return;
    }

    let listBuf = [];
    const flushList = (key) => {
      if (!listBuf.length) return;
      blocks.push(
        <ul key={key} className="my-1 list-disc space-y-0.5 pl-5">
          {listBuf.map((line, i) => <li key={i}>{renderInline(line, `${key}-li${i}`)}</li>)}
        </ul>,
      );
      listBuf = [];
    };

    chunk.split("\n").forEach((line, i) => {
      const key = `t-${idx}-${i}`;
      if (/^##\s+/.test(line)) {
        flushList(`${key}-ul`);
        blocks.push(
          <h3 key={key} className="mb-1 mt-2 text-[13px] font-bold text-zinc-100">
            {renderInline(line.replace(/^##\s+/, ""), key)}
          </h3>,
        );
      } else if (/^-\s+/.test(line)) {
        listBuf.push(line.replace(/^-\s+/, ""));
      } else {
        flushList(`${key}-ul`);
        if (line.trim() === "") {
          blocks.push(<div key={key} className="h-2" />);
        } else {
          blocks.push(<p key={key} className="whitespace-pre-wrap break-words">{renderInline(line, key)}</p>);
        }
      }
    });
    flushList(`end-${idx}-ul`);
  });

  return blocks;
}

/** 完了済みテキスト(エージェントの最終出力・レポート等)を簡易整形して表示する。
 *
 * App は経過時間表示のため毎秒再描画される。text が同じなら再パースしないよう
 * useMemo で固定する(数十万字のレポートをドロワーで開いたまま放置するのが常態のため、
 * 毎秒の全再パースは体感を明確に落とす)。
 */
export default function Markdown({ text, className = "" }) {
  const blocks = useMemo(() => (text ? renderBlocks(text) : null), [text]);
  if (!blocks) return null;
  return <div className={`space-y-0.5 text-[12.5px] leading-relaxed ${className}`}>{blocks}</div>;
}

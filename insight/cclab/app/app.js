// 画面 A〜H の描画と連動。データは data.js(実測・根拠)と model.js(概念モデル)。
(() => {
  const D = window.CC_DATA, M = window.CC_MODEL, T = D.trace;
  const $ = s => document.querySelector(s);
  const factById = new Map(D.facts.map(f => [f.id, f]));
  const CLS = { A: '公式仕様', B: '実測', C: 'ソース', D: '推定', E: '未確認' };
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const badge = c => CLS[c] ? `<span class="cls cls-${c}" title="${CLS[c]}">${c}</span>` : esc(c);
  const fmtT = ms => ms == null ? '—' : `${Math.floor(ms / 60000)}:${((ms % 60000) / 1000).toFixed(1).padStart(4, '0')}`;
  const fmtN = n => n == null ? '—' : Number(n).toLocaleString('ja-JP');
  const state = { mode: 'measured', view: 'arch', built: {}, hotFiles: [] };

  // ---------- 用語: 本文中の語に点線を付け、ホバー/タップで説明を出す ----------
  const GLOSS = D.glossary?.terms || [];
  const GL = new Map(GLOSS.map(g => [g.id, g]));
  const termKeys = GLOSS.flatMap(g => [g.term, ...g.aliases].filter(k => k.length >= 3).map(k => [k, g.id])).sort((a, b) => b[0].length - a[0].length);
  const termRe = termKeys.length ? new RegExp(termKeys.map(([k]) => k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'g') : null;
  const termOf = new Map(termKeys.map(([k, id]) => [k, id]));
  function linkTerms(root) {
    if (!termRe || !root) return;
    const seen = new Set(), walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: n => n.parentElement.closest('.term, a, button, select, option, input, textarea, script, style, svg, .term-pop, h1') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT });
    const nodes = []; while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const n of nodes) {
      const text = n.nodeValue; let last = 0, frag = null;
      text.replace(termRe, (m, off) => {
        const id = termOf.get(m);
        if (seen.has(id)) return m; // 同じ領域では最初の1回だけ印を付ける
        seen.add(id); frag ??= document.createDocumentFragment();
        frag.append(text.slice(last, off));
        const sp = document.createElement('span');
        Object.assign(sp, { className: 'term', tabIndex: 0, textContent: m }); sp.dataset.gid = id;
        frag.append(sp); last = off + m.length; return m;
      });
      if (frag) { frag.append(text.slice(last)); n.replaceWith(frag); }
    }
  }
  const pop = $('#term-pop');
  function showTerm(el) {
    const g = GL.get(el.dataset.gid); if (!g) return;
    pop.innerHTML = `<div class="cat">${esc(g.cat)}</div><h4>${esc(g.term)}</h4><p><b>${esc(g.short)}</b></p><p>${esc(g.detail)}</p><button data-open="${esc(g.id)}">用語集で開く</button>`;
    pop.hidden = false;
    const r = el.getBoundingClientRect();
    pop.style.left = Math.max(8, Math.min(innerWidth - 350, r.left)) + 'px';
    pop.style.top = (r.bottom + 6 + 220 > innerHeight ? Math.max(8, r.top - pop.offsetHeight - 6) : r.bottom + 6) + 'px';
    pop.querySelector('button').onclick = () => { pop.hidden = true; show('glossary'); selectTerm(g.id); };
  }
  document.addEventListener('mouseover', e => { const t = e.target.closest?.('.term'); if (t) showTerm(t); });
  document.addEventListener('focusin', e => { const t = e.target.closest?.('.term'); if (t) showTerm(t); });
  document.addEventListener('click', e => {
    const t = e.target.closest('.term');
    if (t) { e.stopPropagation(); showTerm(t); return; }
    if (!e.target.closest('.term-pop')) pop.hidden = true;
  }, true);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') pop.hidden = true; });

  const allTools = [...T.tools, ...T.subagents.flatMap(s => s.tools.map(t => ({ ...t, subType: s.meta.agentType })))];
  const toolById = new Map(allTools.map(t => [t.id, t]));
  const subByTool = new Map(T.subagents.map(s => [s.meta.toolUseId, s]));
  const sessionEnd = Math.max(...allTools.map(t => t.end ?? t.start), ...T.api_calls.map(c => c.t)) + 2000;
  const toolStatus = t => t.permission?.decision === 'reject' ? 'deny' : t.end == null ? 'run' : t.is_error ? 'err' : 'ok';
  const toolTarget = n => /^(Read|Write|Edit|Glob|Grep|NotebookEdit)$/.test(n) ? 'fs' : /^(Bash|PowerShell|Monitor)$/.test(n) ? 'shell'
    : n === 'Agent' ? 'sub' : n === 'Skill' ? 'skills' : n === 'ToolSearch' ? 'ctx' : /^Web/.test(n) ? 'web' : n.startsWith('mcp__') ? 'mcp' : 'exec';

  // ---------- インスペクター ----------
  function factsHtml(ids = []) {
    const list = ids.map(id => factById.get(id)).filter(Boolean);
    if (!list.length) return '';
    return '<h3>根拠</h3>' + list.map(f => `<div class="fact">${badge(f.cls)}<b>${esc(f.id)}</b> ${esc(f.claim)}
      <div class="src">${f.url ? `<a href="${esc(f.url)}" target="_blank" rel="noopener">${esc(f.url)}</a>` : esc(f.evidence || '')}${f.certainty ? `(${esc(f.certainty)})` : ''}</div></div>`).join('');
  }
  const kv = rows => '<dl class="kv">' + rows.filter(r => r[1] !== undefined && r[1] !== '').map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join('') + '</dl>';
  function inspect(title, html) {
    $('#insp-title').textContent = title;
    $('#insp-body').innerHTML = html;
    linkTerms($('#insp-body'));
    $('#inspector').classList.add('open');
    for (const b of $('#insp-body').querySelectorAll('[data-go]')) b.onclick = () => go(b.dataset);
  }
  $('#insp-close').onclick = () => $('#inspector').classList.remove('open');

  // data-go 属性で画面間をジャンプ
  function go({ go: view, tool, step, fact, file }) {
    show(view);
    if (tool && view === 'tools') selectTool(tool);
    // ジャンプ先の添字は絞り込み前の全イベント基準なので、実験フィルターを解除してから移動する
    if (step != null && view === 'play') { state.mode = 'measured'; syncMode(); $('#p-onlyexp').checked = false; buildSteps(); jumpTo(+step); }
    if (fact && view === 'evidence') { $('#ev-q').value = fact; renderEvidence(); }
    if (file && view === 'files') selectFile(+file);
  }

  // ---------- ナビゲーション ----------
  function show(view) {
    state.view = view;
    for (const b of document.querySelectorAll('#nav > button')) b.classList.toggle('on', b.dataset.view === view);
    for (const s of document.querySelectorAll('.view')) s.classList.toggle('on', s.id === 'view-' + view);
    if (!state.built[view]) { state.built[view] = true; BUILD[view](); }
    $('#nav').classList.remove('open');
  }
  for (const b of document.querySelectorAll('#nav > button')) b.onclick = () => show(b.dataset.view);
  $('#nav-toggle').onclick = () => $('#nav').classList.toggle('open');
  $('#theme').onclick = () => {
    const r = document.documentElement, dark = getComputedStyle(r).colorScheme === 'dark';
    r.dataset.theme = dark ? 'light' : 'dark';
  };
  function syncMode() {
    $('#mode-measured').classList.toggle('on', state.mode === 'measured');
    $('#mode-concept').classList.toggle('on', state.mode === 'concept');
  }
  const setMode = m => { state.mode = m; syncMode(); archBadges(); if (state.built.play) { buildSteps(); jumpTo(0); } };
  $('#mode-measured').onclick = () => setMode('measured');
  $('#mode-concept').onclick = () => setMode('concept');

  $('#ver').textContent = `v${T.session.version}`;
  $('#session-meta').innerHTML = `<div class="legend-title">実測セッション</div>${esc(T.session.entrypoint)}<br>${esc(T.session.id.slice(0, 8))}…<br>${esc(T.session.start_iso)}<br>レコード ${fmtN(T.session.records)} 件`;

  // ---------- 矢印の説明 ----------
  const nodeLabel = id => M.nodes.find(n => n.id === id)?.label || id;
  const edgeById = new Map(M.edges.map(e => [e.id, e]));
  // path の隣り合う2ノードごとに「A → B: 何が流れるか(いつ)」を並べる
  function flowListHtml(path, heading = 'この場面の矢印') {
    const items = [];
    for (let i = 0; i < path.length - 1; i++) {
      const e = edgeById.get(path[i] + '>' + path[i + 1]);
      if (e?.info) items.push(`<li><span class="arrow-tag">${esc(nodeLabel(e.from))} → ${esc(nodeLabel(e.to))}</span><span>${esc(e.info.what)} <span class="when">(${esc(e.info.when)})</span></span></li>`);
    }
    return items.length ? `<h3 style="margin:8px 0 0;font-size:13px">${esc(heading)}</h3><ul class="flow-list">${items.join('')}</ul>` : '';
  }
  function inspectEdge(e) {
    (state.view === 'play' ? playGraph : archGraph)?.highlight([e.from, e.to], { dim: false });
    inspect(`${nodeLabel(e.from)} → ${nodeLabel(e.to)}`, kv([['ラベル', esc(e.label || '—')], ['何が流れるか', esc(e.info?.what)], ['いつ', esc(e.info?.when)], ['画面で見える例', esc(e.info?.eg)]])
      + `<div class="btn-row"><button data-node="${esc(e.from)}">${esc(nodeLabel(e.from))} を見る</button><button data-node="${esc(e.to)}">${esc(nodeLabel(e.to))} を見る</button></div>`);
    for (const b of $('#insp-body').querySelectorAll('[data-node]')) b.onclick = () => inspectNode(M.nodes.find(n => n.id === b.dataset.node));
  }

  // ---------- A: アーキテクチャ ----------
  let archGraph;
  function nodeStats() {
    const cnt = f => T.tools.filter(f).length;
    const hooks = T.tools.reduce((a, t) => a + (t.hooks?.length || 0), 0) + T.events.filter(e => e.kind === 'hook').length;
    return {
      ui: `${T.events.filter(e => e.kind === 'user_prompt').length} 入力`, api: `${T.api_calls.length} 呼出`, loop: `${T.api_calls.length} 周`,
      exec: `${T.tools.length} 件`, fs: `${cnt(t => toolTarget(t.name) === 'fs')}`, shell: `${cnt(t => toolTarget(t.name) === 'shell')}`,
      mcp: `${cnt(t => t.name.startsWith('mcp__'))}`, web: `${cnt(t => toolTarget(t.name) === 'web')}`, skills: `${cnt(t => t.name === 'Skill')}`,
      sub: `${T.subagents.length} 体`, hooks: `${hooks} 回`, perm: `拒否 ${cnt(t => t.permission?.decision === 'reject')}`,
      ctx: `${T.events.filter(e => e.kind === 'attachment').length} 添付`, store: `${fmtN(T.session.records)} 行`,
      memory: `${new Set(T.events.filter(e => e.label === 'instructions').flatMap(e => (e.detail.files || []).map(f => f.path))).size} 件`,
      cache: `再利用 ${(100 * T.api_calls.reduce((a, c) => a + c.cache_read, 0) / Math.max(1, T.api_calls.reduce((a, c) => a + c.context_total, 0))).toFixed(0)}%`,
      compact: `${T.events.filter(e => e.kind === 'system' && /compact/.test(e.label)).length} 回`,
    };
  }
  function archBadges() { archGraph?.setBadges(state.mode === 'measured' ? nodeStats() : {}); }
  function inspectNode(n) {
    archGraph?.highlight([n.id, ...M.edges.filter(e => e.from === n.id).map(e => e.to)], { dim: false });
    const stat = nodeStats()[n.id];
    inspect(n.label, kv([
      ['役割', esc(n.role)], ['入力', esc(n.inputs.join('、'))], ['出力', esc(n.outputs.join('、'))],
      ['関連ファイル', n.files.length ? n.files.map(f => `<code>${esc(f)}</code>`).join('<br>') : '—'],
      ['実測', stat ? `${esc(stat)}(本セッション)` : '—'],
    ]) + `<h3>入ってくる矢印</h3><ul class="flow-list">${M.edges.filter(e => e.to === n.id).map(e => `<li><span class="arrow-tag">${esc(nodeLabel(e.from))} →</span><span>${esc(e.info?.what)}</span></li>`).join('') || '<li>なし</li>'}</ul>`
      + `<h3>出ていく矢印</h3><ul class="flow-list">${M.edges.filter(e => e.from === n.id).map(e => `<li><span class="arrow-tag">→ ${esc(nodeLabel(e.to))}</span><span>${esc(e.info?.what)}</span></li>`).join('') || '<li>なし</li>'}</ul>`
      + factsHtml(n.facts));
  }
  function buildArch() {
    archGraph = new GraphView($('#arch-graph'), { nodes: M.nodes, edges: M.edges, onNodeClick: inspectNode, onEdgeClick: inspectEdge });
    archBadges();
    $('#arch-graph svg').addEventListener('click', () => archGraph.highlight([]));
  }

  // ---------- B: 再生 ----------
  let playGraph;
  const P = { steps: [], i: 0, prog: 0, playing: false, last: 0 };
  const STEP_MS = 1400;

  function measuredSteps() {
    const S = [];
    let buf = null;
    for (const e of T.events) {
      if (e.kind === 'attachment') {
        if (buf && e.t - buf.tEnd < 1500) { buf.items.push(e); buf.tEnd = e.t; continue; }
        buf = { t: e.t, tEnd: e.t, kind: 'attach', items: [e], cls: 'B' };
        S.push(buf);
        continue;
      }
      buf = null;
      if (e.kind === 'user_prompt') S.push({ t: e.t, kind: 'user', cls: 'B', title: 'ユーザー入力', path: ['user', 'ui', 'ctx'], detail: e.detail, facts: ['L1'] });
      else if (e.kind === 'hook') S.push({ t: e.t, kind: 'hook', cls: 'B', title: `フック ${e.label}`, path: ['ui', 'hooks'], detail: e.detail, facts: ['H-01'] });
      else if (e.kind === 'request_shape') S.push({ t: e.t, kind: 'api', cls: 'B', title: `リクエスト形状(tools ${e.detail.tools})`, path: ['ctx'], detail: { system_blocks_chars: e.detail.system_blocks_chars.join(' / '), tools: e.detail.tools, tools_chars: e.detail.tools_chars }, facts: ['B-CTX-2', 'B-CTX-4'] });
      else if (e.kind === 'api_request') S.push({ t: e.t, kind: 'api', cls: 'B', title: 'API リクエスト送信', path: ['ctx', 'api', 'cache'], detail: { model: e.detail.model, effort: e.detail.effort, max_tokens: e.detail.max_tokens, thinking: JSON.stringify(e.detail.thinking), betas: e.detail.betas }, facts: ['B-CTX-5', 'B-CTX-7'] });
      else if (e.kind === 'system') S.push({ t: e.t, kind: 'api', cls: 'B', title: `system: ${e.label}`, path: ['ctx', 'compact', 'ctx'], detail: e.detail, facts: ['B-CMP-1'] });
    }
    for (const s of S) if (s.kind === 'attach') {
      const labels = s.items.map(e => e.label);
      s.title = `文脈への添付 ${s.items.length} 件`;
      s.path = labels.includes('instructions') ? ['memory', 'ctx'] : labels.includes('skill_listing') ? ['skills', 'ctx'] : ['ctx'];
      s.detail = Object.fromEntries(s.items.map(e => [e.label, `${fmtN(e.detail.rendered_chars)} 字${e.detail.files ? ' ' + e.detail.files.map(f => f.path).join(', ') : ''}`]));
      s.files = s.items.flatMap(e => e.detail.files?.map(f => f.path) || []);
      s.facts = ['B-CTX-3', 'B-INS-1'];
    }
    for (const c of T.api_calls) S.push({ t: c.t, kind: 'api', cls: 'B', title: `応答受信 stop_reason=${c.stop_reason}`, path: ['api', 'loop'],
      detail: { model: c.model, '入力合計': fmtN(c.context_total), cache_read: fmtN(c.cache_read), cache_write: fmtN(c.cache_write), output: fmtN(c.output), thinking: c.thinkingMs ? `${fmtN(c.thinkingMs)} ms` : '—', blocks: c.blocks.join(', ') }, facts: ['L7', 'E-1'] });
    for (const t of T.tools) {
      const st = toolStatus(t), tg = toolTarget(t.name);
      const path = st === 'deny' ? ['loop', 'perm', 'loop'] : ['loop', 'perm', 'hooks', 'exec', ...(tg === 'exec' ? [] : [tg])];
      S.push({ t: t.start, kind: 'tool', cls: 'B', title: `tool_use: ${t.name}${t.batch_size > 1 ? `(並列 ${t.batch_size})` : ''}`, path, toolId: t.id, exp: t.exp, files: t.files, tok: st === 'deny' ? 'deny' : '',
        detail: { '権限': t.permission ? `${t.permission.decision}(${t.permission.reasonType}${t.permission.ruleSource ? ' / ' + t.permission.ruleSource : ''})` : '—', 'フック': (t.hooks || []).map(h => `${h.name} ${h.ms}ms`).join(', ') || 'なし', '入力': t.input }, facts: st === 'deny' ? ['B-TOOL-2', 'B-TOOL-3'] : ['R-01', 'H-08'] });
      if (t.end != null) S.push({ t: t.end, kind: st === 'ok' ? 'result' : 'err', cls: 'B', title: `結果: ${t.name} ${st === 'ok' ? '成功' : st === 'deny' ? '拒否' : 'エラー'}(${fmtN(t.ms)} ms)`,
        path: ['exec', 'loop', 'ctx'], toolId: t.id, exp: t.exp, files: t.files, tok: st === 'ok' ? 'ok' : 'err',
        detail: { is_error: String(t.is_error), '出力字数': fmtN(t.output_chars), '出力': t.output }, facts: ['R1', st === 'ok' ? 'B-TOOL-8' : 'B-TOOL-1'] });
      const sub = subByTool.get(t.id);
      if (sub) {
        S.push({ t: sub.start, kind: 'sub', cls: 'B', title: `Subagent ${sub.meta.agentType} が独自ループを開始`, path: ['exec', 'sub', 'api'], exp: t.exp, toolId: t.id,
          detail: { model: sub.models.join(', '), '指示の字数': fmtN(sub.prompt_chars), '起動形態': sub.meta.requestShape }, facts: ['A-06', 'B-SUB-2'] });
        if (sub.end != null) S.push({ t: sub.end, kind: 'sub', cls: 'B', title: `Subagent ${sub.meta.agentType} 完了 → 結果だけを親へ`, path: ['sub', 'exec', 'loop', 'ctx'], exp: t.exp, toolId: t.id,
          detail: { 'ツール使用': sub.tools.length, 'API 呼出': sub.api_calls.length, '出力トークン': fmtN(sub.tokens_out) }, facts: ['A-07', 'B-SUB-5'] });
      }
    }
    const order = { user: 0, hook: 1, attach: 2, api: 3, tool: 4, sub: 5, result: 6, err: 6 };
    return S.sort((a, b) => a.t - b.t || order[a.kind] - order[b.kind]);
  }

  function buildSteps() {
    if (state.mode === 'concept') {
      P.steps = M.steps.map((s, i) => ({ ...s, kind: s.cls === 'E' ? 'api' : 'tool', idx: i + 1 }));
      $('#play-sub').textContent = '概念モデル: 公式仕様と実測から組み直した 22 工程。各工程の分類は主な根拠のもの。';
    } else {
      let s = measuredSteps();
      if ($('#p-onlyexp').checked) s = s.filter(x => x.exp);
      P.steps = s;
      $('#play-sub').textContent = `実測: このセッション(${T.session.id.slice(0, 8)}…)のトランスクリプトから再構成した ${s.length} イベント。時刻はセッション開始からの経過。等間隔で再生。`;
    }
    $('#p-scrub').max = Math.max(0, P.steps.length - 1);
    $('#p-ticks').innerHTML = P.steps.map((s, i) => `<i class="k-${s.kind === 'err' ? 'err' : s.kind === 'attach' ? 'attach' : s.kind === 'sub' ? 'sub' : s.kind === 'api' ? 'api' : 'tool'}" style="left:${(i / Math.max(1, P.steps.length - 1)) * 100}%"></i>`).join('');
    renderLanes();
  }

  function lanesSvg(host, { big = false, onPick } = {}) {
    const W = Math.max(600, host.clientWidth || 800), L = big ? 170 : 120, rowH = big ? 34 : 22;
    const rows = [{ label: 'メイン', start: 0, end: sessionEnd, tools: T.tools, api: T.api_calls }, ...T.subagents.map(s => ({ label: `${s.meta.agentType}`, sub: s, start: s.start, end: s.end ?? sessionEnd, tools: s.tools, api: s.api_calls }))];
    const x = t => L + (W - L - 10) * (t / sessionEnd);
    let h = `<svg viewBox="0 0 ${W} ${rows.length * rowH + 22}" height="${rows.length * rowH + 22}">`;
    rows.forEach((r, i) => {
      const y = i * rowH + 4;
      h += `<text class="lane-label" x="4" y="${y + rowH / 2 + 2}">${esc(r.label)}</text><rect class="lane-bg" x="${L}" y="${y}" width="${W - L - 10}" height="${rowH - 6}" rx="4"/>`;
      h += `<rect class="lane-bar ${r.sub ? 'sub' : ''}" data-i="${i}" x="${x(r.start)}" y="${y}" width="${Math.max(3, x(r.end) - x(r.start))}" height="${rowH - 6}" rx="4"><title>${esc(r.label)} ${fmtT(r.start)}〜${fmtT(r.end)}</title></rect>`;
      for (const c of r.api) h += `<rect class="lane-tick api" x="${x(c.t)}" y="${y + rowH - 10}" width="2" height="4"/>`;
      for (const t of r.tools) h += `<rect class="lane-tick ${toolStatus(t) !== 'ok' ? 'err' : r.sub ? 'sub' : ''}" x="${x(t.start)}" y="${y + 2}" width="${Math.max(2, x(t.end ?? t.start) - x(t.start))}" height="${rowH - 14}"><title>${esc(t.name)} ${fmtT(t.start)}</title></rect>`;
    });
    for (let s = 0; s <= sessionEnd; s += 60000) h += `<text class="axis" x="${x(s)}" y="${rows.length * rowH + 18}">${s / 60000}分</text>`;
    h += `<line class="playhead" x1="-10" x2="-10" y1="0" y2="${rows.length * rowH}"/></svg>`;
    host.innerHTML = h;
    host._x = x;
    if (onPick) for (const el of host.querySelectorAll('.lane-bar')) el.onclick = () => onPick(rows[+el.dataset.i]);
  }

  function renderLanes() {
    if (state.mode === 'concept') { $('#p-lanes').innerHTML = '<p class="muted">概念モデルには時刻がありません。実測データに切り替えると、メインと Subagent の並列実行レーンが表示されます。</p>'; return; }
    lanesSvg($('#p-lanes'));
  }

  function renderStep() {
    const s = P.steps[P.i];
    if (!s) { $('#p-pos').textContent = '0 / 0'; $('#p-now').innerHTML = '<p class="muted">該当する場面がありません。</p>'; playGraph.highlight([]); return; }
    $('#p-scrub').value = P.i;
    $('#p-pos').textContent = `${P.i + 1} / ${P.steps.length}${s.t != null ? ` ・ t=${fmtT(s.t)}` : ''}`;
    playGraph.highlight(s.path, { cls: s.kind });
    playGraph.moveToken(s.path, P.prog, s.tok || (s.kind === 'err' ? 'err' : ''));
    const head = $('#p-lanes .playhead');
    if (head && s.t != null) { const xx = $('#p-lanes')._x(s.t); head.setAttribute('x1', xx); head.setAttribute('x2', xx); }
    const desc = s.detail ? Object.entries(s.detail).map(([k, v]) => `${k}: ${v}`).join(' ・ ') : `${s.actor}: ${s.input} → ${s.output}`;
    $('#p-now').innerHTML = `<h3>${badge(s.cls)}${esc(s.title)}</h3><div class="muted">${esc(desc.slice(0, 400))}</div>${flowListHtml(s.path)}`;
    linkTerms($('#p-now'));
    state.hotFiles = s.files || [];
    markHotFiles();
    stepInspector(s);
  }

  function stepInspector(s) {
    const rows = s.detail ? Object.entries(s.detail).map(([k, v]) => [k, `<span class="mono">${esc(v)}</span>`])
      : [['処理主体', esc(s.actor)], ['入力', esc(s.input)], ['出力', esc(s.output)], ['分岐', esc(s.branch)], ['観測可能性', esc(s.obs)]];
    const extra = s.toolId ? `<div class="btn-row"><button data-go="tools" data-tool="${esc(s.toolId)}">ツールグラフで開く</button></div>` : '';
    inspect(`${s.idx ? s.idx + '. ' : ''}${s.title}`, kv([['時刻', s.t != null ? fmtT(s.t) : '—'], ['経路', esc(s.path.join(' → '))], ...rows,
      ['ファイル', (s.files || []).map(f => `<code>${esc(f)}</code>`).join('<br>')], ['実験', s.exp ? esc(s.exp) : '']]) + flowListHtml(s.path) + extra + factsHtml(s.facts));
  }

  function jumpTo(i) { P.i = Math.max(0, Math.min(P.steps.length - 1, i)); P.prog = 0; renderStep(); }
  function frame(ts) {
    if (!P.playing) return;
    const dt = Math.min(100, ts - P.last); P.last = ts;
    P.prog += dt * +$('#p-speed').value / STEP_MS;
    if (P.prog >= 1) {
      if (P.i < P.steps.length - 1) { P.i++; P.prog = 0; renderStep(); } else { P.prog = 1; setPlaying(false); }
    }
    const s = P.steps[P.i];
    if (!s) { setPlaying(false); return; }
    playGraph.moveToken(s.path, P.prog, s.tok || (s.kind === 'err' ? 'err' : ''));
    requestAnimationFrame(frame);
  }
  function setPlaying(on) {
    if (on && !P.steps.length) return; // 絞り込みで場面が無いときは再生しない
    P.playing = on;
    $('#p-play').textContent = on ? '⏸ 一時停止' : '▶ 再生';
    if (on) { if (P.i >= P.steps.length - 1 && P.prog >= 1) jumpTo(0); P.last = performance.now(); requestAnimationFrame(frame); }
  }
  function buildPlay() {
    playGraph = new GraphView($('#play-graph'), { nodes: M.nodes, edges: M.edges, onNodeClick: n => inspectNode(n), onEdgeClick: inspectEdge });
    $('#p-play').onclick = () => setPlaying(!P.playing);
    $('#p-next').onclick = () => { setPlaying(false); jumpTo(P.i + 1); };
    $('#p-prev').onclick = () => { setPlaying(false); jumpTo(P.i - 1); };
    $('#p-first').onclick = () => { setPlaying(false); jumpTo(0); };
    $('#p-scrub').oninput = e => { setPlaying(false); jumpTo(+e.target.value); };
    $('#p-onlyexp').onchange = () => { setPlaying(false); buildSteps(); jumpTo(0); };
    document.addEventListener('keydown', e => {
      if (state.view !== 'play' || e.target.matches('input, select')) return;
      if (e.key === ' ') { e.preventDefault(); setPlaying(!P.playing); }
      if (e.key === 'ArrowRight') jumpTo(P.i + 1);
      if (e.key === 'ArrowLeft') jumpTo(P.i - 1);
    });
    buildSteps(); jumpTo(0);
  }
  const stepIndexOfTool = id => measuredSteps().findIndex(s => s.toolId === id && s.kind === 'tool');

  // ---------- C: ファイルツリー ----------
  const flatFiles = M.files.flatMap(g => g.items.map(it => ({ ...it, group: g.group, studyOnly: !!g.studyOnly })));
  const envGet = key => key?.split('/').reduce((o, k) => o?.[k], D.env);
  function fileHistory(f) {
    const hits = [];
    if (f.trace) for (const t of allTools) if ((t.files.join(' ') + ' ' + t.input).includes(f.trace)) hits.push({ t: t.start, label: `${t.name}${t.subType ? `(${t.subType})` : ''} ${toolStatus(t)}`, tool: t.id, sub: !!t.subType });
    if (f.inst) {
      // instructions 添付に記録された読込ファイルを、スコープかファイル名で照合する
      const ok = x => (f.inst.scope && x.scope === f.inst.scope) || (f.inst.suffix && String(x.path).endsWith(f.inst.suffix));
      const add = (evs, who) => evs.filter(e => e.label === 'instructions' && (e.detail.files || []).some(ok))
        .forEach(e => hits.push({ t: e.t, label: `instructions 添付(${who})` }));
      add(T.events, 'メイン'); T.subagents.forEach(s => add(s.events, s.meta.agentType));
    }
    return hits.sort((a, b) => a.t - b.t);
  }
  const hasStudy = D.experiments.length > 0;
  const fileGroups = M.files.filter(g => !g.studyOnly || hasStudy);
  // 読込の証拠はこのセッションの記録から決める。記録が無いものは参照調査の記述を補足として出す
  function loadState(f) {
    if (fileHistory(f).length) return ['load', 'このセッションで使用'];
    if (f.studyOnly && /未観測/.test(f.loaded)) return ['mis', '未観測'];
    return envGet(f.key)?.exists === false ? ['no', '不在'] : ['no', '記録なし'];
  }
  function buildFiles() {
    const onlyLoaded = $('#f-loaded').checked;
    $('#file-tree').innerHTML = fileGroups.map(g => `<details open><summary>${esc(g.group)}</summary>${g.items.map(it => {
      const i = flatFiles.findIndex(x => x.path === it.path), ex = envGet(it.key)?.exists, [lc, lt] = loadState(it);
      if (onlyLoaded && lc !== 'load') return '';
      return `<div class="tree-item" data-i="${i}" tabindex="0"><span class="path">${esc(it.path)}</span>
        ${ex === undefined ? '' : `<span class="pill ${ex ? 'yes' : 'no'}">${ex ? '存在' : '不在'}</span>`}<span class="pill ${lc}">${lt}</span></div>`;
    }).join('')}</details>`).join('');
    for (const el of document.querySelectorAll('.tree-item')) {
      el.onclick = () => selectFile(+el.dataset.i);
      el.onkeydown = e => { if (e.key === 'Enter') selectFile(+el.dataset.i); };
    }
    markHotFiles();
  }
  $('#f-loaded').onchange = buildFiles;
  function selectFile(i) {
    const f = flatFiles[i];
    for (const el of document.querySelectorAll('.tree-item')) el.classList.toggle('sel', +el.dataset.i === i);
    const env = envGet(f.key), hist = fileHistory(f);
    const steps = measuredSteps();
    inspect(f.path, kv([
      ['区分', esc(f.group)], ['役割', esc(f.role)], ['読込条件', esc(f.when)], ['タイミング', esc(f.timing)], ['読込主体', esc(f.loader)],
      ['優先順位', esc(f.prec)], ['参照先', esc(f.refs)], ['文脈への影響', esc(f.ctx)], ['トークン', esc(f.tokens)], ['権限・安全', esc(f.sec)],
      ['存在(実測)', env ? (env.exists ? `あり ${env.size != null ? fmtN(env.size) + ' B' : ''} ${esc(env.mtime || '')}` : 'なし') : '—'],
      ['このセッション', esc(loadState(f)[1])], ['参照調査での観測', esc(f.loaded)], ['公式との一致', esc(f.match)],
    ]) + `<h3>${esc(f.traceNote || '読み込み・使用履歴')}(${hist.length})</h3>` + (hist.length ? hist.map(h => {
      const si = h.tool && !h.sub ? steps.findIndex(s => s.toolId === h.tool && s.kind === 'tool') : steps.findIndex(s => s.t >= h.t);
      return `<div class="btn-row"><button data-go="play" data-step="${si}">t=${fmtT(h.t)} ${esc(h.label)}</button>${h.tool ? `<button data-go="tools" data-tool="${esc(h.tool)}">ツール</button>` : ''}</div>`;
    }).join('') : '<p class="muted">このセッションでは記録なし(存在と読込は別)。</p>') + factsHtml(f.facts));
  }
  function markHotFiles() {
    if (!state.built.files) return;
    for (const el of document.querySelectorAll('.tree-item')) {
      const f = flatFiles[+el.dataset.i];
      el.classList.toggle('hot', state.hotFiles.some(p => (f.trace && p.includes(f.trace)) || (f.path === '~/.claude/CLAUDE.md' && /\.claude\\CLAUDE\.md$/.test(p))));
    }
  }

  // ---------- D: コンテキスト ----------
  const agents = [{ id: 'main', label: 'メイン', calls: T.api_calls, events: T.events }, ...T.subagents.map(s => ({ id: s.id, label: `${s.meta.agentType}: ${s.meta.description}`, calls: s.api_calls, events: s.events }))];
  function buildContext() {
    $('#c-agent').innerHTML = agents.map((a, i) => `<option value="${i}">${esc(a.label)}</option>`).join('');
    $('#c-agent').onchange = renderContext;
    renderContext();
  }
  function renderContext() {
    const a = agents[+$('#c-agent').value], calls = a.calls;
    if (!calls.length) { $('#ctx-chart').innerHTML = '<p class="muted">API 呼び出しの記録がありません。</p>'; $('#ctx-stats').innerHTML = ''; $('#ctx-comp').innerHTML = ''; renderToken(); return; }
    const sumIn = calls.reduce((x, c) => x + c.context_total, 0), sumRead = calls.reduce((x, c) => x + c.cache_read, 0);
    const peak = calls.reduce((m, c) => c.context_total > m.context_total ? c : m, calls[0]);
    const stat = (v, k) => `<div class="stat"><div class="v">${v}</div><div class="k">${k}</div></div>`;
    $('#ctx-stats').innerHTML = stat(fmtN(calls.length), 'モデルへの問い合わせ回数')
      + stat(fmtN(calls[0].context_total), '① 固定費(最初の1回に送った量)')
      + stat(fmtN(peak.context_total), `最大の1回(#${calls.indexOf(peak) + 1})`)
      + stat(`${(100 * sumRead / Math.max(1, sumIn)).toFixed(1)}%`, 'キャッシュ再利用率(緑の割合)')
      + stat(fmtN(calls.reduce((x, c) => x + c.output, 0)), 'モデルが書いた量の合計');
    // 上段: 入力の内訳(積み上げ)。下段: 出力(別目盛り)。どちらも API usage の実測値
    const L = 70, R = 20, top = 26, H1 = 260, gapY = 46, H2 = 90;
    const bw = Math.max(8, Math.min(26, 900 / calls.length - 6)), gap = Math.max(3, bw * 0.35);
    const W = Math.max(700, L + R + calls.length * (bw + gap)), Ht = top + H1 + gapY + H2 + 40;
    const nice = v => { const p = 10 ** Math.floor(Math.log10(Math.max(1, v))); return Math.ceil(v / p) * p; };
    const yMaxIn = nice(Math.max(...calls.map(c => c.context_total), 1)), yMaxOut = nice(Math.max(...calls.map(c => c.output), 1));
    const yi = v => top + H1 - H1 * v / yMaxIn, yo = v => top + H1 + gapY + H2 - H2 * v / yMaxOut;
    const xb = i => L + i * (bw + gap);
    const kfmt = v => v >= 1000 ? fmtN(Math.round(v / 100) / 10) + 'k' : fmtN(v);
    let s = `<svg width="${W}" height="${Ht}" role="img" aria-label="API 呼び出しごとの入力と出力のトークン数">`;
    s += `<text class="axis-title" x="${L}" y="14">入力: モデルに送った量(トークン)</text>`;
    for (let k = 0; k <= 4; k++) { const v = yMaxIn * k / 4; s += `<line class="grid" x1="${L}" x2="${W - R}" y1="${yi(v)}" y2="${yi(v)}"/><text class="axis" x="${L - 6}" y="${yi(v) + 4}" text-anchor="end">${kfmt(v)}</text>`; }
    s += `<text class="axis-title" x="${L}" y="${top + H1 + gapY - 8}">出力: モデルが書いた量(トークン・思考を含む)</text>`;
    for (let k = 0; k <= 2; k++) { const v = yMaxOut * k / 2; s += `<line class="grid" x1="${L}" x2="${W - R}" y1="${yo(v)}" y2="${yo(v)}"/><text class="axis" x="${L - 6}" y="${yo(v) + 4}" text-anchor="end">${kfmt(v)}</text>`; }
    // 2回目以降で書込が大きい回はキャッシュの作り直しを疑う
    const spikes = new Set(calls.map((c, i) => i > 0 && c.cache_write > Math.max(20000, 0.3 * c.context_total) ? i : -1).filter(i => i > 0));
    calls.forEach((c, i) => {
      let base = 0;
      for (const [k, cl] of [['cache_read', 's-read'], ['cache_write', 's-write'], ['input', 's-in']]) {
        const v = c[k]; if (!v) continue;
        s += `<rect class="bar ${cl}" data-i="${i}" x="${xb(i)}" y="${yi(base + v)}" width="${bw}" height="${Math.max(1, yi(base) - yi(base + v))}"/>`;
        base += v;
      }
      s += `<rect class="bar s-out" data-i="${i}" x="${xb(i)}" y="${yo(c.output)}" width="${bw}" height="${Math.max(1, yo(0) - yo(c.output))}"/>`;
      if (i % Math.ceil(calls.length / 12) === 0) s += `<text class="axis" x="${xb(i) + bw / 2}" y="${Ht - 22}" text-anchor="middle">${fmtT(c.t)}</text>`;
      if (spikes.has(i)) s += `<text class="anno" x="${xb(i) + bw / 2}" y="${yi(c.context_total) - 6}" text-anchor="middle">⚠</text>`;
    });
    const fy = yi(calls[0].context_total);
    s += `<line class="anno-line" x1="${xb(0) + bw / 2}" x2="${xb(0) + bw / 2}" y1="${fy - 4}" y2="${fy - 14}"/><text class="anno" x="${xb(0)}" y="${Math.max(12, fy - 18)}">① 固定費 ${fmtN(calls[0].context_total)}</text>`;
    s += `<text class="axis-title" x="${L}" y="${Ht - 6}">横軸: 問い合わせの時刻(セッション開始からの経過 分:秒)。1本 = 1回</text></svg>`;
    $('#ctx-chart').innerHTML = s;
    const tip = $('#ctx-tip');
    const tipHtml = (c, i) => `<b>#${i + 1} 回目の問い合わせ(${fmtT(c.t)})</b><br>送った量 合計 <b>${fmtN(c.context_total)}</b><br>`
      + `<i class="sw" style="background:var(--B)"></i> 再利用(キャッシュ読取) ${fmtN(c.cache_read)}<br>`
      + `<i class="sw" style="background:var(--warn)"></i> 新しく追加(キャッシュ書込) ${fmtN(c.cache_write)}<br>`
      + `<i class="sw" style="background:var(--accent)"></i> キャッシュなし ${fmtN(c.input)}<br>`
      + `<i class="sw" style="background:var(--accent2)"></i> モデルが書いた量 ${fmtN(c.output)}${c.thinking_tokens ? `(うち思考 ${fmtN(c.thinking_tokens)})` : ''}<br>`
      + `結果: ${c.stop_reason === 'tool_use' ? 'ツールを使って続行' : c.stop_reason === 'end_turn' ? '回答して終了' : esc(c.stop_reason || '—')}`
      + (spikes.has(i) ? '<br>⚠ 新しく書き込んだ量が多い回(キャッシュの作り直しの疑い)' : '');
    for (const el of $('#ctx-chart').querySelectorAll('.bar')) {
      const i = +el.dataset.i;
      el.onmousemove = e => { tip.innerHTML = tipHtml(calls[i], i); tip.hidden = false; tip.style.left = Math.min(innerWidth - 340, e.clientX + 14) + 'px'; tip.style.top = (e.clientY + 14) + 'px'; };
      el.onmouseleave = () => { tip.hidden = true; };
      el.onclick = () => inspect(`API 呼び出し #${i + 1}`, `<p>${tipHtml(calls[i], i)}</p>` + kv([['モデル', esc(calls[i].model)], ['思考時間', calls[i].thinkingMs ? `${fmtN(calls[i].thinkingMs)} ms` : '—'], ['返したブロック', esc(calls[i].blocks.join(', '))]]) + factsHtml(['P6', 'B-CTX-1', 'E-1']));
    }
    // 最初の呼び出しの構成: 字数は実測、トークンは字数比で総量を按分した推定
    const first = calls[0], shape = a.events.find(e => e.kind === 'request_shape');
    const parts = [];
    if (shape) { shape.detail.system_blocks_chars.forEach((c, i) => parts.push([`システムプロンプト[${i}]`, c, 'system', 'system-prompt'])); parts.push([`ツール定義(${shape.detail.tools} 個)`, shape.detail.tools_chars, 'tools', 'tools-definition']); }
    for (const e of a.events.filter(e => e.kind === 'attachment' && e.t <= first.t && e.detail.rendered_chars)) parts.push([`添付: ${e.label}`, e.detail.rendered_chars, 'attach', GL.has(e.label) ? e.label : 'attachment']);
    for (const e of a.events.filter(e => e.kind === 'user_prompt' && e.t <= first.t)) parts.push(['ユーザーの入力', e.detail.chars, 'user', 'turn']);
    const sum = parts.reduce((x, p) => x + p[1], 0) || 1, total = first.context_total, mx = Math.max(...parts.map(p => p[1]), 1);
    $('#ctx-comp').innerHTML = parts.length ? `<p class="muted">最初の問い合わせで送った ${fmtN(total)} トークン(実測)の中身。字数は記録から数えた実測 ${badge('B')}、トークン数は字数の比で割り振った按分推定 ${badge('D')}。</p>
      <table><tr><th>送ったもの</th><th>これは何か</th><th>字数 ${badge('B')}</th><th style="width:24%">割合</th><th>推定トークン ${badge('D')}</th></tr>${parts.sort((p, q) => q[1] - p[1]).map(p => `<tr><td>${esc(p[0])}</td><td>${esc(GL.get(p[3])?.short || '')}</td><td class="mono">${fmtN(p[1])}</td>
      <td><svg width="100%" height="10" preserveAspectRatio="none" viewBox="0 0 100 10"><rect class="${p[2] === 'tools' ? 's-out' : p[2] === 'system' ? 's-in' : p[2] === 'user' ? 's-write' : 's-read'}" width="${100 * p[1] / mx}" height="10"/></svg></td>
      <td class="mono">≈${fmtN(Math.round(total * p[1] / sum))}</td></tr>`).join('')}</table>` : '<p class="muted">形状レコードがありません。</p>';
    renderToken();
    linkTerms($('#view-context'));
  }

  function renderToken() {
    const tk = D.token;
    if (!tk) { $('#ctx-token').innerHTML = '<p class="muted">トークン分析データがありません。</p>'; return; }
    $('#ctx-token').innerHTML = `<p class="muted">記録から自動で見つけた改善の候補(メインの会話が対象)。削減量は概算 ${badge('D')}。</p>`
      + (tk.candidates.length ? tk.candidates.map(c => `<div class="cand ${esc(c.level)}"><b>${esc(c.title)}</b><div>${esc(c.evidence)}</div><div class="muted">対策: ${esc(c.action)}${c.est_tokens ? ` ・ 概算 ${fmtN(c.est_tokens)} トークン` : ''}</div></div>`).join('') : '<p>目立つ候補はありません。</p>');
  }

  // ---------- E: ツールグラフ ----------
  let selTool = null;
  function buildTools() {
    $('#t-exp').innerHTML += D.experiments.filter(e => e.tools.length).map(e => `<option value="${e.id}">${e.id}: ${esc(e.title)}</option>`).join('');
    for (const id of ['#t-exp', '#t-scale', '#t-sub']) $(id).onchange = renderTools;
    renderTools();
  }
  function renderTools() {
    const exp = $('#t-exp').value, scale = +$('#t-scale').value;
    let rows = $('#t-sub').checked ? allTools : T.tools;
    if (exp) rows = rows.filter(t => t.exp === exp || (exp === 'G' && t.subType === 'worker'));
    rows = [...rows].sort((a, b) => a.start - b.start);
    if (!rows.length) { $('#tool-gantt').innerHTML = '<p class="muted">該当する呼び出しはありません。</p>'; return; }
    const t0 = Math.min(...rows.map(t => t.start)), t1 = Math.max(...rows.map(t => t.end ?? t.start)) + 500;
    const host = $('#tool-gantt'), L = 250, rh = 20, W = Math.max(700, (host.clientWidth - 20) * scale);
    const x = t => L + (W - L - 20) * (t - t0) / Math.max(1, t1 - t0);
    let s = `<svg width="${W}" height="${rows.length * rh + 30}">`;
    const step = [1, 2, 5, 10, 30, 60, 120, 300].map(v => v * 1000).find(v => (t1 - t0) / v < 12 * scale) || 600000;
    for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) s += `<line class="grid" x1="${x(t)}" x2="${x(t)}" y1="0" y2="${rows.length * rh}"/><text class="axis" x="${x(t) + 2}" y="${rows.length * rh + 14}">${fmtT(t)}</text>`;
    // 同一応答(message.id)の並列バッチを帯で囲む
    const seen = new Set();
    rows.forEach((t, i) => {
      if (t.batch_size < 2 || seen.has(t.msg)) return;
      seen.add(t.msg);
      const idx = rows.map((r, j) => r.msg === t.msg ? j : -1).filter(j => j >= 0);
      s += `<rect class="g-batch" x="2" y="${Math.min(...idx) * rh}" width="${W - 4}" height="${(Math.max(...idx) - Math.min(...idx) + 1) * rh}" rx="4"><title>並列バッチ(同一 API 応答の tool_use ${idx.length} 件)</title></rect>`;
    });
    rows.forEach((t, i) => {
      const y = i * rh, st = toolStatus(t), w = Math.max(3, x(t.end ?? t.start + 300) - x(t.start));
      s += `<g class="g-tool" data-id="${esc(t.id)}"><rect class="g-row" x="0" y="${y}" width="${W}" height="${rh}" fill="transparent"/>
        <text class="g-label ${t.subType ? 'sub' : ''}" x="4" y="${y + 14}">${t.exp ? `[${t.exp}] ` : ''}${esc(t.subType ? '↳ ' : '')}${esc(t.name.length > 30 ? t.name.slice(0, 29) + '…' : t.name)}</text>
        <rect class="g-${st} ${t.id === selTool ? 'g-sel' : ''}" x="${x(t.start)}" y="${y + 4}" width="${w}" height="${rh - 8}" rx="3"><title>${esc(t.name)} ${fmtN(t.ms)} ms</title></rect>
        <text class="axis" x="${x(t.start) + w + 4}" y="${y + 14}">${t.ms != null ? fmtN(t.ms) + 'ms' : ''}</text></g>`;
    });
    host.innerHTML = s + '</svg>';
    for (const g of host.querySelectorAll('.g-tool')) g.onclick = () => selectTool(g.dataset.id);
  }
  function selectTool(id) {
    selTool = id;
    if (state.built.tools) renderTools();
    const t = toolById.get(id);
    if (!t) return;
    const e = D.experiments.find(x => x.id === t.exp), si = t.subType ? -1 : stepIndexOfTool(id);
    inspect(`${t.name}`, kv([
      ['実行者', t.subType ? `Subagent(${esc(t.subType)})` : 'メイン'], ['実験', e ? `${e.id}: ${esc(e.title)}` : '—'],
      ['開始 / 終了', `${fmtT(t.start)} / ${fmtT(t.end)}`], ['処理時間', t.ms != null ? `${fmtN(t.ms)} ms` : '未完了'],
      ['実行形態', t.batch_size > 1 ? `並列バッチ(同一応答 ${t.batch_size} 件)` : '単独'], ['結果', toolStatus(t)],
      ['権限判定', t.permission ? esc(JSON.stringify(t.permission)) : '—'],
      ['フック', (t.hooks || []).map(h => `${esc(h.name)} ${h.ms}ms exit ${h.exit}`).join('<br>') || 'なし'],
      ['関連ファイル', t.files.map(f => `<code>${esc(f)}</code>`).join('<br>') || '—'],
    ]) + `<h3>入力</h3><pre class="log">${esc(t.input)}</pre><details class="fold" ${t.output?.length < 300 ? 'open' : ''}><summary>出力(${fmtN(t.output_chars)} 字・先頭のみ)</summary><pre class="log">${esc(t.output)}</pre></details>`
      + (e?.observed ? `<p>${badge('B')}${esc(e.observed)}</p>` : '') + (si >= 0 ? `<div class="btn-row"><button data-go="play" data-step="${si}">処理の再生で表示</button></div>` : '')
      + factsHtml(toolStatus(t) === 'deny' ? ['B-TOOL-2', 'B-TOOL-3'] : toolStatus(t) === 'err' ? ['B-TOOL-1', 'B-TOOL-5', 'R4'] : ['R1', 'R2']));
  }

  // ---------- F: Subagent ----------
  function buildSubagents() {
    lanesSvg($('#sub-lanes'), { big: true, onPick: r => r.sub ? inspectSub(r.sub) : null });
    const mainShape = T.events.find(e => e.kind === 'request_shape'), att = evs => new Set(evs.filter(e => e.kind === 'attachment').map(e => e.label));
    const mainAtt = att(T.events);
    const cols = [{ name: 'メイン', type: '—', model: [...new Set(T.api_calls.map(c => c.model))].join(', '), origin: 'ユーザー', prompt: '—',
      sys: mainShape ? mainShape.detail.system_blocks_chars.reduce((a, b) => a + b, 0) : null, tools: mainShape?.detail.tools, att: mainAtt, calls: T.api_calls, ntools: T.tools.length, span: [0, sessionEnd], ret: '—' },
    ...T.subagents.map(s => { const sh = s.events.find(e => e.kind === 'request_shape'); return { name: s.meta.description, type: s.meta.agentType, model: s.models.join(', '), origin: `Agent ツール(${s.meta.requestShape}、深さ ${s.meta.spawnDepth})`, prompt: `${fmtN(s.prompt_chars)} 字`,
      sys: sh ? sh.detail.system_blocks_chars.reduce((a, b) => a + b, 0) : null, tools: sh?.detail.tools, att: att(s.events), calls: s.api_calls, ntools: s.tools.length, span: [s.start, s.end], ret: '最終テキストのみ(task-notification)' }; })];
    const has = (c, k) => c.att.has(k) ? '✓' : '—';
    const rowsDef = [['種類', c => esc(c.type)], ['モデル', c => esc(c.model)], ['起動元・条件', c => esc(c.origin)], ['渡された指示', c => c.prompt], ['system 字数', c => fmtN(c.sys)], ['ツール数', c => fmtN(c.tools)],
      ['CLAUDE.md 添付', c => has(c, 'instructions')], ['スキル一覧', c => has(c, 'skill_listing')], ['エージェント一覧', c => has(c, 'agent_listing_delta')], ['遅延ツール通知', c => has(c, 'deferred_tools_delta')],
      ['API 呼び出し', c => fmtN(c.calls.length)], ['最初の呼出 cache read / write', c => c.calls[0] ? `${fmtN(c.calls[0].cache_read)} / ${fmtN(c.calls[0].cache_write)}` : '—'],
      ['出力トークン計', c => fmtN(c.calls.reduce((a, x) => a + x.output, 0))], ['ツール使用', c => fmtN(c.ntools)], ['実行期間', c => `${fmtT(c.span[0])}〜${fmtT(c.span[1])}`], ['親への返却', c => esc(c.ret)]];
    $('#sub-table').innerHTML = `<table><tr><th>項目</th>${cols.map(c => `<th>${esc(c.name)}</th>`).join('')}</tr>${rowsDef.map(([k, f]) => `<tr><td>${k}</td>${cols.map(c => `<td>${f(c)}</td>`).join('')}</tr>`).join('')}</table>
      <p class="muted" style="padding:0 8px">${badge('B')} 上表はすべて本セッションのトランスクリプトから算出。親の文脈に入るのは「親への返却」だけ(${badge('A')} A-07)。</p>`;
  }
  function inspectSub(s) {
    inspect(`Subagent: ${s.meta.agentType}`, kv([['説明', esc(s.meta.description)], ['起動した tool_use', `<code>${esc(s.meta.toolUseId)}</code>`], ['モデル', esc(s.models.join(', '))],
      ['期間', `${fmtT(s.start)}〜${fmtT(s.end)}`], ['ツール', `${s.tools.length} 回: ${esc([...new Set(s.tools.map(t => t.name))].join(', '))}`], ['API 呼出', s.api_calls.length],
      ['isSidechain', String(s.isSidechain)], ['添付の種類', esc(s.attachment_types.join(', '))]]) + `<div class="btn-row"><button data-go="tools" data-tool="${esc(s.meta.toolUseId)}">起動した Agent 呼び出し</button></div>` + factsHtml(['A-06', 'A-07', 'B-SUB-1', 'B-SUB-2', 'B-SUB-3', 'B-SUB-4', 'B-CTX-6']));
  }

  // ---------- G: エラーフロー ----------
  let errGraph;
  function buildErrors() {
    $('#err-chips').innerHTML = M.errors.map((e, i) => `<button data-i="${i}">${badge(e.cls)}${esc(e.title)}</button>`).join('');
    for (const b of $('#err-chips').querySelectorAll('button')) b.onclick = () => renderError(+b.dataset.i);
    renderError(0);
  }
  function renderError(i) {
    const f = M.errors[i];
    for (const b of $('#err-chips').querySelectorAll('button')) b.classList.toggle('on', +b.dataset.i === i);
    // 先頭から辿り、最初の子は同じ行の次の列、2つ目以降の子は下の行へ置く
    const pos = new Map(), order = [f.states[0][0]];
    pos.set(order[0], [0, 0]);
    let nextRow = 1;
    for (let q = 0; q < order.length; q++) {
      const [c, r] = pos.get(order[q]);
      f.links.filter(l => l[0] === order[q] && !pos.has(l[1])).forEach((l, k) => { pos.set(l[1], [c + 1, k === 0 ? r : nextRow++]); order.push(l[1]); });
    }
    const nodes = f.states.map(([id, label, kind]) => { const [c, r] = pos.get(id) || [0, nextRow++]; return { id, label, kind, group: 'core', x: 120 + c * 210, y: 80 + r * 110 }; });
    const edges = f.links.map(([a, b, label]) => ({ id: a + '>' + b, from: a, to: b, label: label || '' }));
    errGraph?.destroy();
    errGraph = new GraphView($('#err-graph'), { nodes, edges, nodeW: 176, nodeH: 58, minimap: false, onNodeClick: n => errGraph.highlight([n.id]) });
    let k = 0;
    clearInterval(renderError.timer);
    renderError.timer = setInterval(() => { // 状態遷移を順に光らせる
      if (state.view !== 'errors') return;
      const path = order.slice(0, (k++ % order.length) + 1);
      errGraph.highlight(path, { dim: false });
    }, 900);
    const t = f.tool && allTools.find(x => x.id.endsWith(f.tool));
    inspect(f.title, kv([['分類', `${badge(f.cls)}${CLS[f.cls]}`], ['状態数', f.states.length], ['実測した呼び出し', t ? `${esc(t.name)} ${fmtT(t.start)} ${toolStatus(t)}` : 'なし(隔離環境で安全に再現できないため公式仕様で説明)']])
      + (t ? `<div class="btn-row"><button data-go="tools" data-tool="${esc(t.id)}">実測の呼び出しを開く</button></div>` : '') + factsHtml(f.facts));
  }

  // ---------- H: 根拠 ----------
  const evCls = new Set(['A', 'B', 'C', 'D', 'E']);
  function buildEvidence() {
    const counts = Object.fromEntries(Object.keys(CLS).map(c => [c, D.facts.filter(f => f.cls === c).length]));
    $('#ev-summary').textContent = `全 ${D.facts.length} 件: ` + Object.entries(counts).map(([c, n]) => `${c} ${CLS[c]} ${n}`).join(' / ');
    $('#ev-cls').innerHTML = Object.keys(CLS).map(c => `<button data-c="${c}" class="on">${badge(c)}${CLS[c]}(${counts[c]})</button>`).join('');
    for (const b of $('#ev-cls').querySelectorAll('button')) b.onclick = () => { evCls.has(b.dataset.c) ? evCls.delete(b.dataset.c) : evCls.add(b.dataset.c); b.classList.toggle('on'); renderEvidence(); };
    $('#ev-topic').innerHTML += [...new Set(D.facts.map(f => f.topic))].sort().map(t => `<option>${esc(t)}</option>`).join('');
    $('#ev-topic').onchange = renderEvidence; $('#ev-q').oninput = renderEvidence;
    $('#ev-unconf').innerHTML = D.unconfirmed.map(u => `<li>${badge('E')}${esc(u)}</li>`).join('');
    renderEvidence();
  }
  function renderEvidence() {
    if (!state.built.evidence) return;
    const q = $('#ev-q').value.trim().toLowerCase(), tp = $('#ev-topic').value;
    const list = D.facts.filter(f => evCls.has(f.cls) && (!tp || f.topic === tp) && (!q || (f.id + ' ' + f.claim + ' ' + (f.url || f.evidence)).toLowerCase().includes(q)));
    $('#ev-table').innerHTML = `<table><tr><th>分類</th><th>ID</th><th>トピック</th><th>内容</th><th>根拠</th></tr>${list.map(f => `<tr class="click" data-id="${esc(f.id)}"><td>${badge(f.cls)}</td><td class="mono">${esc(f.id)}</td><td>${esc(f.topic)}</td><td>${esc(f.claim)}</td>
      <td class="mono">${f.url ? `<a href="${esc(f.url)}" target="_blank" rel="noopener">${esc(f.url.replace('https://code.claude.com/docs/en/', 'docs/'))}</a>${f.certainty ? `<br>${esc(f.certainty)}` : ''}` : esc(f.evidence)}</td></tr>`).join('')}</table>`;
    for (const r of $('#ev-table').querySelectorAll('tr.click')) r.onclick = () => inspect(r.dataset.id, factsHtml([r.dataset.id]));
    linkTerms($('#ev-table'));
  }

  // ---------- I: 用語集 ----------
  let glCat = '';
  function buildGlossary() {
    const cats = D.glossary?.categories || [];
    $('#gl-cats').innerHTML = `<button data-c="" class="on">すべて(${GLOSS.length})</button>` + cats.map(c => `<button data-c="${esc(c)}">${esc(c)}(${GLOSS.filter(g => g.cat === c).length})</button>`).join('');
    for (const b of $('#gl-cats').querySelectorAll('button')) b.onclick = () => { glCat = b.dataset.c; for (const x of $('#gl-cats').querySelectorAll('button')) x.classList.toggle('on', x === b); renderGlossary(); };
    $('#gl-q').oninput = renderGlossary;
    renderGlossary();
  }
  function renderGlossary() {
    const q = $('#gl-q').value.trim().toLowerCase();
    const list = GLOSS.filter(g => (!glCat || g.cat === glCat) && (!q || [g.term, ...g.aliases, g.short, g.detail].join(' ').toLowerCase().includes(q)));
    $('#gl-list').innerHTML = list.map(g => `<article class="gl-card" id="gl-${esc(g.id)}"><div class="muted" style="font-size:11.5px">${esc(g.cat)}</div><h3>${esc(g.term)}</h3>
      ${g.aliases.length ? `<div class="alias">別名: ${esc(g.aliases.join('、'))}</div>` : ''}<p class="short">${esc(g.short)}</p><p class="detail">${esc(g.detail)}</p>
      <div class="rel">${g.related.filter(r => GL.has(r)).map(r => `<button data-rel="${esc(r)}">→ ${esc(GL.get(r).term)}</button>`).join('')}${g.facts.filter(f => factById.has(f)).map(f => `<button data-fact="${esc(f)}">${badge(factById.get(f).cls)}${esc(f)}</button>`).join('')}</div></article>`).join('') || '<p class="muted">該当なし</p>';
    for (const b of $('#gl-list').querySelectorAll('[data-rel]')) b.onclick = () => { glCat = ''; $('#gl-q').value = ''; renderGlossary(); selectTerm(b.dataset.rel); };
    for (const b of $('#gl-list').querySelectorAll('[data-fact]')) b.onclick = () => inspect(b.dataset.fact, factsHtml([b.dataset.fact]));
  }
  function selectTerm(id) {
    if (!state.built.glossary) show('glossary');
    let el = document.getElementById('gl-' + id);
    if (!el) { glCat = ''; $('#gl-q').value = ''; renderGlossary(); el = document.getElementById('gl-' + id); }
    for (const c of document.querySelectorAll('.gl-card')) c.classList.toggle('sel', c === el);
    el?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }

  // ---------- 検索 ----------
  const index = [
    ...M.nodes.map(n => ({ kind: 'ノード', text: `${n.label} ${n.sub} ${n.role}`, label: `${n.label}(${n.sub})`, act: () => { show('arch'); archGraph.focusNode(n.id); inspectNode(n); } })),
    ...flatFiles.map((f, i) => ({ kind: 'ファイル', text: `${f.path} ${f.role}`, label: f.path, act: () => { show('files'); selectFile(i); } })),
    ...D.facts.map(f => ({ kind: `根拠 ${f.cls}`, text: `${f.id} ${f.claim}`, label: `${f.id} ${f.claim}`, act: () => { show('evidence'); $('#ev-q').value = f.id; renderEvidence(); inspect(f.id, factsHtml([f.id])); } })),
    ...GLOSS.map(g => ({ kind: '用語', text: `${g.term} ${g.aliases.join(' ')} ${g.short}`, label: `${g.term} — ${g.short}`, act: () => { show('glossary'); selectTerm(g.id); } })),
    ...allTools.map(t => ({ kind: 'ツール', text: `${t.name} ${t.input} ${t.description || ''}`, label: `${t.name} ${fmtT(t.start)} ${t.description || ''}`, act: () => { show('tools'); selectTool(t.id); } })),
  ];
  $('#search').oninput = e => {
    const q = e.target.value.trim().toLowerCase(), box = $('#search-results');
    if (!q) { box.hidden = true; return; }
    const hits = index.filter(x => x.text.toLowerCase().includes(q)).slice(0, 14);
    box.innerHTML = hits.length ? hits.map((h, i) => `<button data-i="${i}"><span class="muted">${esc(h.kind)}</span> ${esc(h.label.slice(0, 90))}</button>`).join('') : '<p class="muted" style="padding:8px">該当なし</p>';
    box.hidden = false;
    for (const b of box.querySelectorAll('button')) b.onclick = () => { box.hidden = true; hits[+b.dataset.i].act(); };
  };
  document.addEventListener('click', e => { if (!e.target.closest('.search')) $('#search-results').hidden = true; });

  const BUILD = { arch: buildArch, play: buildPlay, files: buildFiles, context: buildContext, tools: buildTools, subagents: buildSubagents, errors: buildErrors, evidence: buildEvidence, glossary: buildGlossary };
  for (const p of document.querySelectorAll('.view-head p')) linkTerms(p);
  show('arch');
})();

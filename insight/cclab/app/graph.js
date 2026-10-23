// 依存なしの SVG グラフ表示: ノード/エッジ描画、ホイールズーム、ドラッグパン、ミニマップ、強調、移動トークン。
const SVGNS = 'http://www.w3.org/2000/svg';
const NODE_W = 168, NODE_H = 54;

function svgEl(tag, attrs = {}, parent) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (parent) parent.appendChild(el);
  return el;
}

class GraphView {
  constructor(host, { nodes, edges, onNodeClick, onEdgeClick, nodeW = NODE_W, nodeH = NODE_H, minimap = true }) {
    this.host = host; this.nodes = nodes; this.edges = edges; this.onNodeClick = onNodeClick; this.onEdgeClick = onEdgeClick;
    this.nw = nodeW; this.nh = nodeH;
    this.view = { x: 0, y: 0, k: 1 };
    host.classList.add('graph-host');
    this.svg = svgEl('svg', { class: 'graph-svg' }, host);
    const defs = svgEl('defs', {}, this.svg);
    for (const [id, cls] of [['arrow', 'arrow'], ['arrow-hot', 'arrow hot']]) {
      const m = svgEl('marker', { id: host.id + '-' + id, viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse' }, defs);
      svgEl('path', { d: 'M0,0 L10,5 L0,10 z', class: cls }, m);
    }
    this.root = svgEl('g', {}, this.svg);
    this.edgeLayer = svgEl('g', {}, this.root);
    this.nodeLayer = svgEl('g', {}, this.root);
    this.tokenLayer = svgEl('g', {}, this.root);
    this.nodeEls = new Map(); this.edgeEls = new Map();
    this.draw();
    if (minimap) this.buildMinimap();
    this.bindPanZoom();
    const zoomBar = document.createElement('div');
    zoomBar.className = 'zoom-bar';
    zoomBar.innerHTML = '<button title="拡大">＋</button><button title="縮小">－</button><button title="全体表示">⤢</button>';
    const [zi, zo, zf] = zoomBar.querySelectorAll('button');
    zi.onclick = () => this.zoomAt(1.25); zo.onclick = () => this.zoomAt(0.8); zf.onclick = () => this.fit();
    host.appendChild(zoomBar);
    this.ro = new ResizeObserver(() => this.fit());
    this.ro.observe(host);
  }

  destroy() {
    this.ro.disconnect();
    this.host.replaceChildren();
  }

  nodeById(id) { return this.nodes.find(n => n.id === id); }

  edgeGeom(e) {
    const a = this.nodeById(e.from), b = this.nodeById(e.to);
    const dx = b.x - a.x, dy = b.y - a.y;
    const reverse = this.edges.some(o => o.from === e.to && o.to === e.from);
    // 双方向エッジは左右にずらして重ならないようにする
    const off = reverse ? 9 : 0, len = Math.hypot(dx, dy) || 1;
    const ox = -dy / len * off, oy = dx / len * off;
    const clip = (n, tx, ty) => {
      const sx = Math.abs(tx) > 1e-6 ? (this.nw / 2) / Math.abs(tx) : Infinity;
      const sy = Math.abs(ty) > 1e-6 ? (this.nh / 2) / Math.abs(ty) : Infinity;
      const s = Math.min(sx, sy);
      return [n.x + tx * s, n.y + ty * s];
    };
    const [x1, y1] = clip(a, dx / len, dy / len), [x2, y2] = clip(b, -dx / len, -dy / len);
    return { x1: x1 + ox, y1: y1 + oy, x2: x2 + ox, y2: y2 + oy };
  }

  draw() {
    for (const e of this.edges) {
      const g = this.edgeGeom(e);
      const grp = svgEl('g', { class: 'edge' }, this.edgeLayer);
      const line = svgEl('path', { d: `M${g.x1},${g.y1} L${g.x2},${g.y2}`, 'marker-end': `url(#${this.host.id}-arrow)` }, grp);
      // 細い線は狙いにくいので、透明な太い線をクリック・ホバー用に重ねる
      const hit = svgEl('path', { d: `M${g.x1},${g.y1} L${g.x2},${g.y2}`, class: 'edge-hit' }, grp);
      if (e.info) svgEl('title', {}, hit).textContent = `${this.nodeById(e.from).label} → ${this.nodeById(e.to).label}: ${e.info.what}`;
      hit.addEventListener('click', ev => { ev.stopPropagation(); this.onEdgeClick?.(e); });
      if (e.label) {
        const t = svgEl('text', { x: (g.x1 + g.x2) / 2, y: (g.y1 + g.y2) / 2 - 4, class: 'edge-label' }, grp);
        t.textContent = e.label;
      }
      this.edgeEls.set(e.id, { grp, line, geom: g });
    }
    for (const n of this.nodes) {
      const grp = svgEl('g', { class: `node grp-${n.group || 'core'} kind-${n.kind || ''}`, transform: `translate(${n.x - this.nw / 2},${n.y - this.nh / 2})`, tabindex: 0, role: 'button' }, this.nodeLayer);
      svgEl('rect', { width: this.nw, height: this.nh, rx: 8 }, grp);
      const lines = String(n.label).split('\n');
      lines.forEach((ln, i) => {
        const t = svgEl('text', { x: this.nw / 2, y: (n.sub ? 22 : this.nh / 2 + 5 - (lines.length - 1) * 8) + i * 16, class: 'node-label' }, grp);
        t.textContent = ln;
      });
      if (n.sub) { const s = svgEl('text', { x: this.nw / 2, y: 40, class: 'node-sub' }, grp); s.textContent = n.sub; }
      const badge = svgEl('text', { x: this.nw - 6, y: 13, class: 'node-badge' }, grp);
      grp.addEventListener('click', ev => { ev.stopPropagation(); this.onNodeClick?.(n); });
      grp.addEventListener('keydown', ev => { if (ev.key === 'Enter') this.onNodeClick?.(n); });
      this.nodeEls.set(n.id, { grp, badge });
    }
  }

  setBadges(map) {
    for (const [id, { badge }] of this.nodeEls) badge.textContent = map[id] ?? '';
  }

  bounds() {
    const xs = this.nodes.map(n => n.x), ys = this.nodes.map(n => n.y);
    return { x0: Math.min(...xs) - this.nw, y0: Math.min(...ys) - this.nh, x1: Math.max(...xs) + this.nw, y1: Math.max(...ys) + this.nh };
  }

  fit() {
    const r = this.host.getBoundingClientRect();
    if (!r.width || !r.height) return;
    const b = this.bounds();
    const k = Math.min(r.width / (b.x1 - b.x0), r.height / (b.y1 - b.y0)) * 0.95;
    this.view = { k, x: (r.width - (b.x1 + b.x0) * k) / 2, y: (r.height - (b.y1 + b.y0) * k) / 2 };
    this.apply();
  }

  zoomAt(f, cx, cy) {
    const r = this.host.getBoundingClientRect();
    cx ??= r.width / 2; cy ??= r.height / 2;
    const k = Math.max(0.2, Math.min(4, this.view.k * f));
    const s = k / this.view.k;
    this.view = { k, x: cx - (cx - this.view.x) * s, y: cy - (cy - this.view.y) * s };
    this.apply();
  }

  apply() {
    this.root.setAttribute('transform', `translate(${this.view.x},${this.view.y}) scale(${this.view.k})`);
    this.updateMinimap();
  }

  bindPanZoom() {
    this.svg.addEventListener('wheel', ev => {
      ev.preventDefault();
      const r = this.host.getBoundingClientRect();
      this.zoomAt(ev.deltaY < 0 ? 1.12 : 0.89, ev.clientX - r.left, ev.clientY - r.top);
    }, { passive: false });
    let drag = null;
    this.svg.addEventListener('pointerdown', ev => {
      if (ev.target.closest('.node')) return;
      drag = { x: ev.clientX, y: ev.clientY, vx: this.view.x, vy: this.view.y };
      this.svg.setPointerCapture(ev.pointerId); this.svg.classList.add('panning');
    });
    this.svg.addEventListener('pointermove', ev => {
      if (!drag) return;
      this.view.x = drag.vx + ev.clientX - drag.x; this.view.y = drag.vy + ev.clientY - drag.y; this.apply();
    });
    const end = () => { drag = null; this.svg.classList.remove('panning'); };
    this.svg.addEventListener('pointerup', end); this.svg.addEventListener('pointercancel', end);
  }

  buildMinimap() {
    const b = this.bounds();
    this.mini = svgEl('svg', { class: 'minimap', viewBox: `${b.x0} ${b.y0} ${b.x1 - b.x0} ${b.y1 - b.y0}` });
    this.host.appendChild(this.mini);
    for (const n of this.nodes) svgEl('rect', { x: n.x - this.nw / 2, y: n.y - this.nh / 2, width: this.nw, height: this.nh, rx: 6, class: 'mini-node', 'data-id': n.id }, this.mini);
    this.miniView = svgEl('rect', { class: 'mini-view' }, this.mini);
    this.mini.addEventListener('pointerdown', ev => {
      const pt = this.mini.createSVGPoint(); pt.x = ev.clientX; pt.y = ev.clientY;
      const p = pt.matrixTransform(this.mini.getScreenCTM().inverse());
      this.centerOn(p.x, p.y);
    });
  }

  centerOn(x, y) {
    const r = this.host.getBoundingClientRect();
    this.view.x = r.width / 2 - x * this.view.k; this.view.y = r.height / 2 - y * this.view.k; this.apply();
  }

  updateMinimap() {
    if (!this.miniView) return;
    const r = this.host.getBoundingClientRect(), { x, y, k } = this.view;
    this.miniView.setAttribute('x', -x / k); this.miniView.setAttribute('y', -y / k);
    this.miniView.setAttribute('width', r.width / k); this.miniView.setAttribute('height', r.height / k);
  }

  // path: 通過するノード ID 列。存在するエッジだけを強調する
  highlight(path = [], { dim = true, cls = '' } = {}) {
    const onNodes = new Set(path), onEdges = new Set();
    for (let i = 0; i < path.length - 1; i++) onEdges.add(path[i] + '>' + path[i + 1]);
    for (const [id, { grp }] of this.nodeEls) {
      grp.classList.toggle('hot', onNodes.has(id));
      grp.classList.toggle('dim', dim && path.length > 0 && !onNodes.has(id));
      grp.classList.toggle('last', id === path[path.length - 1]);
      grp.dataset.state = onNodes.has(id) ? cls : '';
    }
    for (const [id, { grp, line }] of this.edgeEls) {
      const on = onEdges.has(id);
      grp.classList.toggle('hot', on);
      grp.classList.toggle('dim', dim && path.length > 0 && !on);
      line.setAttribute('marker-end', `url(#${this.host.id}-${on ? 'arrow-hot' : 'arrow'})`);
    }
    for (const el of this.mini?.querySelectorAll('.mini-node') || []) el.classList.toggle('hot', onNodes.has(el.dataset.id));
  }

  // 進捗 p(0..1)で path に沿ってトークンを動かす
  moveToken(path, p, kind = '') {
    this.tokenLayer.replaceChildren();
    const segs = [];
    for (let i = 0; i < path.length - 1; i++) {
      const e = this.edgeEls.get(path[i] + '>' + path[i + 1]);
      if (e) segs.push(e.geom);
    }
    if (!segs.length) {
      const n = this.nodeById(path[0]);
      if (n) svgEl('circle', { cx: n.x, cy: n.y - this.nh / 2 - 8, r: 6 + 3 * Math.sin(p * Math.PI * 4), class: `token pulse ${kind}` }, this.tokenLayer);
      return;
    }
    const f = Math.min(segs.length - 1e-6, p * segs.length), g = segs[Math.floor(f)], t = f - Math.floor(f);
    svgEl('circle', { cx: g.x1 + (g.x2 - g.x1) * t, cy: g.y1 + (g.y2 - g.y1) * t, r: 7, class: `token ${kind}` }, this.tokenLayer);
  }

  focusNode(id) {
    const n = this.nodeById(id);
    if (!n) return;
    this.centerOn(n.x, n.y);
    this.highlight([id]);
  }
}
window.GraphView = GraphView;

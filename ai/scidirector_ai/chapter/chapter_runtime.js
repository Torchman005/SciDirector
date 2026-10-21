'use strict';
/* =========================================================
   SciDirector 章节运行时（受信任）
   - 画面是时间 t（秒，章节时间）的纯函数：__seek(t) 任意顺序调用结果一致
   - 时间锚点来自整章旁白的逐字时间戳：T(id) 句首、TE(id) 句末、K(id,'词') 说到该词
   - 导演代码只调用这里导出的函数，用 scene(首句id, build) 注册场景
   ========================================================= */
(() => {
const DATA = JSON.parse(document.getElementById('scid-chapter-v1').textContent);
const TIMING = DATA.timing, DUR = TIMING.dur, W0 = DATA.width, H0 = DATA.height;
const LINES = TIMING.lines, BY = {};
LINES.forEach((l, i) => { l.i = i; BY[l.id] = l; });
const errors = window.__errors = [];
const fail = msg => { errors.push(msg); throw new Error(msg); };
const line = id => BY[id] || fail(`未知旁白 id：${id}`);

/* ---------- 时间 ---------- */
const T = id => line(id).start, TE = id => line(id).end;
const norm = s => String(s).replace(/[\s　，。！？、；：,.!?;:…—\-–·“”‘’"'（）()《》〈〉【】\[\]{}<>/\\|~～*#]+/g, '').toLowerCase();
/** 说到某个词的时刻；n 表示第几次出现（0 起）。找不到即报错，防止节拍静默错位。 */
const K = (id, word, n = 0) => {
  const l = line(id), w = norm(word); let at = -1;
  for (let k = 0; k <= n; k++) { at = l.chars.indexOf(w, at + 1); if (at < 0) fail(`旁白 ${id} 中没有第 ${n + 1} 个“${word}”`); }
  return l.times[at];
};
/** 说完某个词的时刻。 */
const KE = (id, word, n = 0) => {
  const l = line(id), w = norm(word), start = K(id, word, n), at = l.times.indexOf(start);
  const last = at + w.length; return last < l.times.length ? l.times[last] : l.end;
};

/* ---------- 数学与缓动 ---------- */
const clamp = (x, a = 0, b = 1) => x < a ? a : x > b ? b : x;
const lerp = (a, b, p) => a + (b - a) * p;
const Ez = {
  io: p => p < .5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2,
  out: p => 1 - Math.pow(1 - p, 3),
  back: p => { const c1 = 1.70158, c3 = c1 + 1; return 1 + c3 * Math.pow(p - 1, 3) + c1 * Math.pow(p - 1, 2); },
  lin: p => p,
  sin: p => -(Math.cos(Math.PI * p) - 1) / 2,
};
/** 从 a 秒开始、历时 d 秒的 0→1 进度。 */
const P = (t, a, d = .6, e = Ez.io) => e(clamp((t - a) / d));
/** a 处淡入、b 处淡出的窗口。 */
const W = (t, a, b, d = .5, dOut = d) => P(t, a, d) * (1 - P(t, b, dOut));
const hex = c => { const v = c.replace('#', ''); return [0, 2, 4].map(i => parseInt(v.slice(i, i + 2), 16)); };
const mix = (a, b, p) => { const x = hex(a), y = hex(b); return '#' + x.map((v, i) => Math.round(lerp(v, y[i], clamp(p))).toString(16).padStart(2, '0')).join(''); };
const softmax = xs => { const m = Math.max(...xs), e = xs.map(x => Math.exp(x - m)), s = e.reduce((a, b) => a + b, 0); return e.map(v => v / s); };
function rng(seed) { return () => { seed |= 0; seed = seed + 0x6D2B79F5 | 0; let x = Math.imul(seed ^ seed >>> 15, 1 | seed); x = x + Math.imul(x ^ x >>> 7, 61 | x) ^ x; return ((x ^ x >>> 14) >>> 0) / 4294967296; }; }

/* ---------- 调色板 ---------- */
const C = Object.assign({ paper: '#F6EFDD', card: '#FFFBF1', ink: '#2E2B3A', mute: '#8C8497', line: '#D9C9A6',
  q: '#E8735A', k: '#2C9F95', v: '#6B62D6', gold: '#F0B23E', blue: '#3A6FD8' }, DATA.palette || {});

/* ---------- SVG 基础 ---------- */
const NS = 'http://www.w3.org/2000/svg';
const f2 = v => typeof v === 'number' ? +v.toFixed(3) : v;
function S(tag, attrs = {}, parent) {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) { if (k === 'text') e.textContent = attrs[k]; else e.setAttribute(k, f2(attrs[k])); }
  if (parent) parent.appendChild(e); return e;
}
const A = (e, o) => { for (const k in o) e.setAttribute(k, f2(o[k])); return e; };
const O = (e, v) => { e.setAttribute('opacity', clamp(v).toFixed(3)); return e; };
const TR = (e, x, y, s = 1, r = 0) => { e.setAttribute('transform', `translate(${x.toFixed(2)},${y.toFixed(2)}) rotate(${r.toFixed(2)}) scale(${(+s).toFixed(4)})`); return e; };
const TRS = (e, x, y, sx, sy) => { e.setAttribute('transform', `translate(${x.toFixed(2)},${y.toFixed(2)}) scale(${Math.max(sx, .001).toFixed(4)},${Math.max(sy, .001).toFixed(4)})`); return e; };
const FONT = DATA.font || '"Microsoft YaHei","PingFang SC","Noto Sans CJK SC",sans-serif';
const txt = (parent, x, y, s, attrs = {}) => S('text', Object.assign({ x, y, text: s, fill: C.ink, 'font-size': 32, 'dominant-baseline': 'central' }, attrs), parent);
/** 描线路径：DP(e, p) 画到 p。 */
const drawPath = (parent, d, attrs = {}) => S('path', Object.assign({ d, fill: 'none', pathLength: 1, 'stroke-dasharray': '1 1', 'stroke-dashoffset': 1, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }, attrs), parent);
const DP = (e, p) => { e.setAttribute('stroke-dashoffset', (1 - clamp(p)).toFixed(4)); e.setAttribute('visibility', p <= .001 ? 'hidden' : 'visible'); return e; };
const measureCtx = document.createElement('canvas').getContext('2d');
const textW = (s, fs, weight = 700) => { measureCtx.font = `${weight} ${fs}px ${FONT}`; return measureCtx.measureText(s).width; };

/* ---------- 组件 ---------- */
/** 圆角词卡：中心对齐，返回 {g,r,t,w,h}。 */
function chip(parent, label, o = {}) {
  const fs = o.fs || 40, w = Math.max(textW(label, fs) + fs * .9, fs * 1.6), h = o.h || fs * 1.6;
  const g = S('g', {}, parent);
  S('rect', { x: -w / 2 + 3, y: -h / 2 + 5, width: w, height: h, rx: h * .28, fill: '#000', opacity: .07 }, g);
  const r = S('rect', { x: -w / 2, y: -h / 2, width: w, height: h, rx: h * .28, fill: o.fill || C.card, stroke: o.stroke || C.line, 'stroke-width': o.sw || 2.5 }, g);
  const t = txt(g, 0, 1, label, { 'text-anchor': 'middle', 'font-size': fs, 'font-weight': 700, fill: o.color || C.ink });
  return { g, r, t, w, h };
}
/** 一行词卡的中心 x（按真实字宽排布）。 */
function rowXs(labels, fs, gap, cx) {
  const ws = labels.map(l => Math.max(textW(l, fs) + fs * .9, fs * 1.6));
  const total = ws.reduce((a, b) => a + b, 0) + gap * (labels.length - 1);
  let x = cx - total / 2;
  return ws.map(w => { const c = x + w / 2; x += w + gap; return c; });
}
/** 面板：带柔和阴影的圆角卡片。 */
function panel(parent, x, y, w, h, o = {}) {
  const g = S('g', {}, parent);
  S('rect', { x: x + 4, y: y + 8, width: w, height: h, rx: 22, fill: '#6B4B3B', opacity: .07 }, g);
  S('rect', { x, y, width: w, height: h, rx: 22, fill: o.fill || C.card, stroke: o.stroke || C.line, 'stroke-width': 2 }, g);
  if (o.title) txt(g, x + 32, y + 42, o.title, { 'font-size': o.fs || 30, 'font-weight': 700, fill: o.color || C.ink });
  return g;
}
/** 章节标题：编号徽标 + 标题。 */
function heading(svg, num, title) {
  const g = S('g', {}, svg);
  S('rect', { x: 56, y: 92, width: 58, height: 40, rx: 10, fill: C.ink }, g);
  txt(g, 85, 113, num, { 'text-anchor': 'middle', 'font-size': 24, 'font-weight': 800, fill: C.card });
  txt(g, 132, 113, title, { 'font-size': 36, 'font-weight': 800 });
  return g;
}
/** 两点间的弧线（h>0 向上拱）。 */
const arcD = (x1, x2, y, h) => `M${x1} ${y} Q ${(x1 + x2) / 2} ${y - h * 2} ${x2} ${y}`;
const arcPt = (x1, x2, y, h, s) => { const cx = (x1 + x2) / 2, cy = y - h * 2, u = 1 - s; return [u * u * x1 + 2 * u * s * cx + s * s * x2, u * u * y + 2 * u * s * cy + s * s * y]; };
/** 沿路径流动的小点：update(t, ptFn(s)->[x,y], visible)。 */
function flowDots(parent, n, color, r = 6) {
  const dots = Array.from({ length: n }, () => S('circle', { r, fill: color, opacity: 0 }, parent));
  return { dots, update(t, pt, vis, speed = .45, phase = 0) {
    dots.forEach((d, k) => { const s = ((t * speed + k / n + phase) % 1 + 1) % 1; const [x, y] = pt(s); A(d, { cx: x, cy: y }); O(d, vis * Math.sin(Math.PI * s)); });
  } };
}
/** 一排向量格子：返回格子数组；color(i) 决定颜色。 */
function vecRow(parent, x, y, n, color, o = {}) {
  const c = o.cell || 26, gap = o.gap || 4;
  return Array.from({ length: n }, (_, i) => S('rect', { x: x + i * (c + gap), y, width: c, height: c, rx: 5, fill: color(i), stroke: o.stroke || 'none' }, parent));
}
/** 热力矩阵：set(values[i][j] ∈ [0,1], p) 以 p 混合到目标色。 */
function matrix(parent, o) {
  const { x, y, rows, cols } = o, c = o.cell || 44, gap = o.gap || 4, hot = o.color || C.blue, cold = o.cold || '#F3ECDC';
  const g = S('g', {}, parent);
  const cells = Array.from({ length: rows }, (_, i) => Array.from({ length: cols }, (_, j) =>
    S('rect', { x: x + j * (c + gap), y: y + i * (c + gap), width: c, height: c, rx: 6, fill: cold }, g)));
  (o.rowLabels || []).forEach((l, i) => txt(g, x - 14, y + i * (c + gap) + c / 2, l, { 'text-anchor': 'end', 'font-size': o.fs || 24, fill: C.mute, 'font-weight': 700 }));
  (o.colLabels || []).forEach((l, j) => txt(g, x + j * (c + gap) + c / 2, y - 22, l, { 'text-anchor': 'middle', 'font-size': o.fs || 24, fill: C.mute, 'font-weight': 700 }));
  return { g, cells, cell: c, gap, set(values, p = 1) { cells.forEach((row, i) => row.forEach((e, j) => A(e, { fill: mix(cold, hot, clamp(values[i][j]) * p) }))); } };
}
/** 柱状图：update(values, p, highlight) — values 已归一化到 [0,1]，可在两组数据间插值。 */
function bars(parent, o) {
  const { x, y, w, h, labels } = o, n = labels.length, bw = o.bar || Math.min(46, w / n * .62), step = w / n;
  const g = S('g', {}, parent);
  S('line', { x1: x - 8, y1: y + h, x2: x + w, y2: y + h, stroke: C.line, 'stroke-width': 2 }, g);
  const rects = labels.map((_, i) => S('rect', { x: x + i * step + (step - bw) / 2, y: y + h, width: bw, height: 0, rx: 6, fill: o.color || C.k }, g));
  const vals = labels.map((_, i) => txt(g, x + i * step + step / 2, y + h - 16, '', { 'text-anchor': 'middle', 'font-size': o.vfs || 22, 'font-weight': 700, fill: C.ink, opacity: 0 }));
  labels.forEach((l, i) => txt(g, x + i * step + step / 2, y + h + 26, l, { 'text-anchor': 'middle', 'font-size': o.fs || 22, fill: C.mute, 'font-weight': 700 }));
  return { g, rects, update(values, p = 1, o2 = {}) {
    rects.forEach((r, i) => { const v = clamp(values[i]) * p, bh = Math.max(v * h, p > 0 ? 3 : 0);
      A(r, { y: y + h - bh, height: bh, fill: o2.color ? o2.color(i) : (o.color || C.k) });
      const show = o2.labelAbove == null ? values[i] > .08 : o2.labelAbove(i);
      A(vals[i], { y: y + h - bh - 18 }); vals[i].textContent = `${Math.round(values[i] * 100)}%`; O(vals[i], show ? p : 0); });
  } };
}
/** 弹出：scale 与透明度一起变化。 */
const pop = (e, x, y, p, base = 1) => { TR(e, x, y, base * Math.max(p, 0)); O(e, clamp(p * 1.6)); };

/* ---------- 场景与成片框架 ---------- */
const stage = document.getElementById('stage');
const scenesRoot = document.getElementById('scenes');
const scenes = [], pending = [];
/** 注册场景：从首句前 0.75 秒淡入，到下一场景首句前 0.35 秒淡出。 */
function scene(firstId, build) { pending.push({ firstId, build }); }
function buildScenes() {
  pending.forEach(({ firstId, build }, k) => {
    const div = document.createElement('div'); div.className = 'scene'; scenesRoot.appendChild(div);
    const svg = S('svg', { viewBox: `0 0 ${W0} ${H0}` }, div);
    const a = k === 0 ? -10 : T(firstId) - .75;
    const b = k === pending.length - 1 ? DUR + 30 : T(pending[k + 1].firstId) - .35;
    const sc = { a, b, div, svg };
    sc.update = build(svg, sc) || (() => {});
    scenes.push(sc);
  });
}

const HL = DATA.highlights || {};
const esc = s => s.replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' })[c]);
const hlKeys = Object.keys(HL).sort((a, b) => b.length - a.length);
const hlRe = hlKeys.length ? new RegExp(hlKeys.map(k => k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'g') : null;
const subFmt = s => hlRe ? esc(s).replace(hlRe, m => `<em style="color:${HL[m]}">${m}</em>`) : esc(s);
const sub = document.getElementById('sub'), prog = document.getElementById('prog');
let lastSub = -1;

function render(t) {
  for (const sc of scenes) {
    const vis = t > sc.a - .05 && t < sc.b + .6;
    if (!vis) { sc.div.style.display = 'none'; continue; }
    sc.div.style.display = 'block';
    const i = P(t, sc.a, .75), o = 1 - P(t, sc.b, .6), v = i * o;
    sc.div.style.opacity = v.toFixed(3);
    sc.div.style.transform = `translateY(${((1 - i) * 26 - (1 - o) * 26).toFixed(1)}px) scale(${(.985 + .015 * v).toFixed(4)})`;
    sc.div.style.filter = v < .98 ? `blur(${((1 - v) * 5).toFixed(2)}px)` : 'none';
    sc.update(t);
  }
  let li = -1;
  for (let k = 0; k < LINES.length; k++) if (t >= LINES[k].start - .12 && t <= LINES[k].end + .25) { li = k; break; }
  if (li >= 0) {
    if (li !== lastSub) { sub.innerHTML = subFmt(LINES[li].sub); lastSub = li; }
    const l = LINES[li], v = P(t, l.start - .12, .2) * (1 - P(t, l.end + .05, .2));
    sub.style.opacity = v.toFixed(3);
    sub.style.transform = `translateY(${((1 - P(t, l.start - .12, .3, Ez.out)) * 10).toFixed(1)}px)`;
  } else sub.style.opacity = '0';
  prog.style.width = (clamp(t / DUR) * 100).toFixed(3) + '%';
}

window.__ready = false;
window.__seek = seconds => {
  try { render(Math.max(0, Math.min(DUR, Number.isFinite(seconds) ? seconds : 0))); }
  catch (e) { if (errors.length < 20) errors.push(String(e && e.stack || e)); }
};
window.SC = { DATA, DUR, W: W0, H: H0, C, T, TE, K, KE, clamp, lerp, Ez, P, Wn: W, mix, softmax, rng,
  S, A, O, TR, TRS, txt, drawPath, DP, textW, chip, rowXs, panel, heading, arcD, arcPt, flowDots, vecRow, matrix, bars, pop, scene };
window.__chapterStart = () => {
  const fonts = document.fonts && document.fonts.ready ? document.fonts.ready.catch(() => null) : Promise.resolve();
  fonts.then(() => {
    try { buildScenes(); render(0); window.__ready = true; }
    catch (e) { errors.push(String(e && e.stack || e)); window.__ready = true; }
  });
};
})();

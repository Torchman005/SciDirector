/* SciDirector adapter. Upstream motion helpers and Canvas contract are loaded as FYM/FYC. */
(() => {
'use strict';
const D = JSON.parse(document.getElementById('scid-fy-data').textContent);
const {timing, width: W, height: H} = D, DUR = timing.dur, M = FYM;
const C = {...D.palette}, V = Object.freeze(D.vars || {});
const by = new Map(timing.lines.map(l => [l.id, l]));
const errors = window.__errors = [], scenes = [];
let textBoxes = [], activeId = '', film, themed = D.explicit_palette === true, started = false;
window.__ready = false;
const fail = message => { throw new Error(message); };
function theme(palette) {
  if (D.explicit_palette) fail('palette 已由服务端设置；场景代码不要再调用 theme()，请修改输出 JSON 的 palette 字段');
  if (started || themed || scenes.length || window.__sceneTag !== D.first_ids[0]) fail('theme 只能在首场景注册前调用一次');
  const keys = Object.keys(C);
  if (!palette || typeof palette !== 'object' || Array.isArray(palette)) fail('theme 配色必须为颜色对象');
  const supplied = Object.keys(palette), missing = keys.filter(k => !Object.hasOwn(palette, k));
  const extra = supplied.filter(k => !keys.includes(k));
  const invalid = keys.filter(k => Object.hasOwn(palette, k) &&
    (typeof palette[k] !== 'string' || !/^#(?:[0-9a-f]{3}|[0-9a-f]{4}|[0-9a-f]{6}|[0-9a-f]{8})$/i.test(palette[k].trim())));
  if (missing.length || extra.length || invalid.length) fail('theme 配色无效：' +
    [missing.length ? '缺少键=' + missing.join(',') : '', extra.length ? '多余键=' + extra.join(',') : '',
     invalid.length ? '非法颜色键=' + invalid.join(',') : ''].filter(Boolean).join('；') +
    '。必须完整提供 paper/card/ink/mute/line/q/k/v/gold/blue，颜色使用 #RGB/#RGBA/#RRGGBB/#RRGGBBAA');
  for (const key of keys) C[key] = palette[key].trim();
  themed = true;
}
const line = id => by.get(id) || fail(`未知旁白 id：${id}`);
const norm = value => String(value).replace(/[\s，。！？、；：,.!?;:…—“”‘’"'（）()《》]+/g, '').toLowerCase();
const T = id => line(id).start, TE = id => line(id).end;
function wordIndex(id, word, occurrence) {
  const value = norm(word), source = line(id).chars;
  if (!value || !Number.isInteger(occurrence) || occurrence < 0) fail('关键词或 occurrence 无效');
  let at = -1;
  for (let i = 0; i <= occurrence; i++) {
    at = source.indexOf(value, at + 1);
    if (at < 0) fail(`旁白 ${id} 中没有第 ${occurrence + 1} 个“${word}”`);
  }
  return [at, value.length];
}
const K = (id, word, n = 0) => line(id).times[wordIndex(id, word, n)[0]];
const KE = (id, word, n = 0) => {
  const [at, length] = wordIndex(id, word, n);
  return line(id).times[at + length] ?? TE(id);
};
const canvas = document.getElementById('film'), ctx = canvas.getContext('2d');
const originalText = ctx.fillText.bind(ctx);
// Record measured glyph bounds, including current camera/rotation/scale. This is
// a text layout check; arbitrary Canvas shapes and relationship semantics need review.
ctx.fillText = function(text, x, y, maxWidth) {
  if (maxWidth !== undefined) fail('文字不可用 maxWidth 压扁；改为换行或重新排版');
  const metrics = ctx.measureText(String(text)), tr = ctx.getTransform();
  const corners = [[x - metrics.actualBoundingBoxLeft, y - metrics.actualBoundingBoxAscent],
    [x + metrics.actualBoundingBoxRight, y + metrics.actualBoundingBoxDescent]];
  const [a, b] = corners;
  const points = [[a[0], a[1]], [a[0], b[1]], [b[0], a[1]], [b[0], b[1]]]
    .map(([px, py]) => new DOMPoint(px, py).matrixTransform(tr));
  const xs = points.map(p => p.x), ys = points.map(p => p.y);
  if (ctx.globalAlpha > .15 && String(text).trim()) textBoxes.push({scene: activeId, text: String(text),
    x: Math.min(...xs), y: Math.min(...ys), right: Math.max(...xs), bottom: Math.max(...ys),
    size: (parseFloat(ctx.font.match(/([\d.]+)px/)?.[1]) || 0) * Math.min(Math.hypot(tr.a, tr.b), Math.hypot(tr.c, tr.d))});
  originalText(text, x, y);
};
function label(text, x, y, {size = 36, color = C.ink, weight = 600, align = 'center'} = {}) {
  ctx.save(); ctx.font = `${weight} ${size}px ${D.font}`;
  ctx.fillStyle = color; ctx.textAlign = align; ctx.textBaseline = 'middle';
  ctx.fillText(String(text), x, y); ctx.restore();
}
function box(x, y, w, h, {fill = C.card, stroke = C.line, radius = 18, lineWidth = 2} = {}) {
  ctx.beginPath(); ctx.roundRect(x, y, w, h, radius);
  ctx.fillStyle = fill; ctx.fill(); ctx.lineWidth = lineWidth; ctx.strokeStyle = stroke; ctx.stroke();
}
function scene(id, draw) {
  if (!D.first_ids.includes(id) || scenes.some(s => s.id === id) || typeof draw !== 'function') fail(`场景注册无效：${id}`);
  scenes.push({id, draw});
}
const api = {M, C, V, W, H, DUR, T, TE, K, KE, label, box, scene, theme};
window.FY = Object.freeze(api);
window.__fyLayout = () => {
  const issues = [], add = (b, message) => issues.push(`[场景 ${b.scene}] ${message}：“${b.text.slice(0, 40)}”`);
  for (const b of textBoxes) {
    if (![b.x, b.y, b.right, b.bottom, b.size].every(Number.isFinite)) add(b, '文字边界非有限值');
    if (b.size < (D.min_font || 25)) add(b, '文字字号过小');
    const margin = W * .02, top = D.chapter_chrome === false ? H * .03 : 100;
    const bottom = D.chapter_chrome === false ? H * .85 : H - 205;
    if (b.x < margin || b.right > W - margin || b.y < top || b.bottom > bottom) add(b, '文字出画或进入标题/字幕区域');
    const a = D.presenter_area;
    if (a && b.right > a.x*W && b.x < (a.x+a.width)*W && b.bottom > a.y*H && b.y < (a.y+a.height)*H)
      add(b, '文字进入讲解员避让区域');
  }
  for (let i = 0; i < textBoxes.length; i++) for (let j = i + 1; j < textBoxes.length; j++) {
    const a = textBoxes[i], b = textBoxes[j];
    if (a.scene === b.scene && Math.min(a.right, b.right) - Math.max(a.x, b.x) > 3 &&
        Math.min(a.bottom, b.bottom) - Math.max(a.y, b.y) > 3) add(a, `独立文字重叠（另一个：${b.text.slice(0, 30)}）`);
  }
  return issues;
};
function render({t}) {
  textBoxes = []; activeId = '';
  ctx.fillStyle = C.paper; ctx.fillRect(0, 0, W, H);
  for (let i = 0; i < scenes.length; i++) {
    const s = scenes[i], start = i === 0 ? 0 : T(s.id), end = i + 1 < scenes.length ? T(scenes[i + 1].id) : DUR;
    if (t < start || (i + 1 < scenes.length && t >= end)) continue;
    activeId = s.id; ctx.save();
    try { s.draw({...api, ctx, t, local: t - start}); }
    catch (error) { const message = `[场景 ${s.id}] 绘制失败：${error.message}`; if (!errors.includes(message)) errors.push(message); throw error; }
    finally { ctx.restore(); }
  }
  const current = timing.lines.find(l => t >= l.start && t < l.end);
  if (D.chapter_chrome !== false) {
    document.getElementById('sub').textContent = current?.sub || '';
    document.getElementById('progress').style.width = `${100 * t / DUR}%`;
  }
}
window.__fyStart = async () => {
  started = true; Object.freeze(C);
  // A script error already explains why registration stopped. Do not obscure it
  // with derivative "missing theme / unregistered scene" diagnostics.
  if (errors.length) { window.__ready = true; return; }
  if (D.require_theme && !themed) errors.push(`[场景 ${D.first_ids[0]}] 所选画风缺少 theme 配色`);
  const root = document.documentElement.style;
  for (const key of ['paper', 'ink', 'mute', 'blue']) root.setProperty(`--fy-${key}`, C[key]);
  for (const id of D.first_ids) if (!scenes.some(s => s.id === id)) errors.push(`[场景 ${id}] 未注册`);
  scenes.sort((a, b) => D.first_ids.indexOf(a.id) - D.first_ids.indexOf(b.id));
  film = FYC.createCanvasContract({canvas, width: W, height: H, duration: DUR, fps: D.fps,
    draw: render, resources: document.fonts.ready});
  try { await film.readyPromise; film.seek(0); window.__ready = true; }
  catch (error) { errors.push(`初始化失败：${error.message}`); window.__ready = true; }
};
window.__seek = t => {
  if (errors.length) fail(errors.join('\n'));
  return film.seek(t);
};
window.__fyPixels = () => canvas.toDataURL('image/png');
window.addEventListener('error', e => errors.push((window.__sceneTag ? `[场景 ${window.__sceneTag}] ` : '') + '脚本错误：' + e.message));
})();

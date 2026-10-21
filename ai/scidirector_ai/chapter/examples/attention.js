// 注意力机制章节 —— 导演代码。只使用 SC 运行时导出的函数；所有画面都是 t 的纯函数。
const TOK = ['小猫', '没有', '喝', '牛奶', '因为', '它', '太', '烫', '了'];
const TOKC = ['#F29A4A', '#A3A9B5', '#7DBA74', '#4F9BE0', '#B29EDB', '#EDAA2F', '#E58FA8', '#E46B55', '#9DB39A'];
const IT = 5, IMILK = 3, ICAT = 0, IHOT = 7;
// 示意分数：q(它)·k_j，d = 64。
const RAW = [10, 2, 4, 26, 1, 6, 3, 12, 1];
const W_RAW = softmax(RAW), W_SC = softmax(RAW.map(x => x / 8));

/* =========================================================
   场景 1：一个“它”引发的问题
   ========================================================= */
scene('hello', svg => {
  const hd = heading(svg, '01', '一个“它”引发的问题');
  const title = S('g', {}, svg);
  txt(title, 960, 440, '注意力机制', { 'text-anchor': 'middle', 'font-size': 132, 'font-weight': 900, 'letter-spacing': 14 });
  txt(title, 960, 540, 'Attention Mechanism', { 'text-anchor': 'middle', 'font-size': 44, class: 'math', fill: C.mute });
  const ul = drawPath(title, 'M650 600 C 770 585, 860 615, 960 598 S 1160 585, 1270 602', { stroke: C.gold, 'stroke-width': 11 });
  const blobs = [[C.blue, 560, 400, 170], [C.q, 1360, 330, 120], [C.k, 1240, 650, 90], [C.gold, 640, 700, 70]]
    .map(([c, x, y, r]) => S('circle', { cx: x, cy: y, r, fill: c, opacity: 0 }, svg));

  const fs = 48, Y = 520, xs = rowXs(TOK, fs, 22, 760).map((x, i) => x + (i >= 4 ? 18 : -18));
  const arcMilk = drawPath(svg, arcD(xs[IT], xs[IMILK], Y - 48, 95), { stroke: C.blue, 'stroke-width': 6 });
  const arcCat = drawPath(svg, arcD(xs[IT], xs[ICAT], Y - 48, 135), { stroke: TOKC[ICAT], 'stroke-width': 6 });
  const depth = j => -(30 + Math.abs(xs[j] - xs[IT]) * .12);
  const down = TOK.map((_, j) => j === IT ? null : drawPath(svg, arcD(xs[j], xs[IT], Y + 48, depth(j)), { stroke: TOKC[j], 'stroke-width': 4, opacity: .85 }));
  const dots = TOK.map((_, j) => j === IT ? null : flowDots(svg, 3, TOKC[j]));
  const chips = TOK.map(l => chip(svg, l, { fs }));
  const hungry = chip(svg, '饿', { fs, fill: '#FFF0EA', stroke: C.q });
  const comma = txt(svg, (xs[3] + xs[4]) / 2, Y + 14, '，', { 'text-anchor': 'middle', 'font-size': 48, 'font-weight': 700 });
  const period = txt(svg, xs[8] + 52, Y + 14, '。', { 'text-anchor': 'middle', 'font-size': 48, 'font-weight': 700 });
  const lblMilk = txt(svg, (xs[IT] + xs[IMILK]) / 2, Y - 48 - 190 - 26, '它 = 牛奶', { 'text-anchor': 'middle', class: 'hand', 'font-size': 42, fill: C.blue, 'font-weight': 700 });
  const lblCat = txt(svg, (xs[IT] + xs[ICAT]) / 2, Y - 48 - 270 - 26, '它 = 小猫', { 'text-anchor': 'middle', class: 'hand', 'font-size': 42, fill: '#E07F2A', 'font-weight': 700 });
  const bubble = S('g', {}, svg);
  S('circle', { cx: 0, cy: 0, r: 40, fill: C.gold }, bubble);
  txt(bubble, 0, 2, '?', { 'text-anchor': 'middle', 'font-size': 52, 'font-weight': 900, fill: '#fff' });
  S('path', { d: 'M-10 34 L0 56 L10 34 Z', fill: C.gold }, bubble);
  const ring = S('circle', { r: 78, fill: 'none', stroke: C.gold, 'stroke-width': 4, 'stroke-dasharray': '10 14', opacity: 0 }, svg);
  const ctx = txt(svg, xs[IT], Y + 180, '上下文 context', { 'text-anchor': 'middle', 'font-size': 28, fill: C.mute, 'letter-spacing': 3 });
  const cap = txt(svg, 760, 800, '注意力：让每个词根据上下文，更新自己的含义', { 'text-anchor': 'middle', class: 'hand', 'font-size': 46, 'font-weight': 700 });
  const capU = drawPath(svg, 'M330 832 L1190 832', { stroke: C.gold, 'stroke-width': 8, opacity: .55 });

  return t => {
    const ts = T('sent');
    const ti = P(t, .15, 1.0, Ez.out), to = P(t, ts - .45, .7);
    O(title, ti * (1 - to));
    title.setAttribute('transform', `translate(0,${((1 - ti) * 30 - to * 40).toFixed(1)})`);
    DP(ul, P(t, .9, 1.0));
    blobs.forEach((b, k) => { O(b, .09 * ti * (1 - .6 * to)); TR(b, Math.sin(t * .5 + k * 2) * 24, Math.cos(t * .4 + k) * 18); });
    O(hd, P(t, ts - .2, .6));

    const tw = T('who'), tm = K('milk', '牛奶'), tf = K('hungry', '饿'), tcat = K('hungry', '小猫'), tc = T('context');
    const dim = P(t, tw, .5) * (1 - P(t, tc, .6));
    const hlMilk = P(t, tm, .4) * (1 - P(t, tf - .2, .4));
    const hlCat = P(t, tcat, .4) * (1 - P(t, tc + .3, .5));
    chips.forEach((c, i) => {
      // 每个词在被念到的那一刻弹出。
      const p = P(t, K('sent', TOK[i]) - .08, .5, Ez.back);
      let y = Y + (1 - p) * 40, s = .6 + .4 * p, op = clamp(p * 2), sy = 1;
      if (i === IT) {
        const lift = P(t, tw, .5, Ez.back) * (1 - P(t, tc + .2, .6));
        y -= 22 * lift;
        A(c.r, { fill: mix(C.card, '#FFE7B0', lift), stroke: mix(C.line, C.gold, lift) });
      } else {
        const hl = i === IMILK ? hlMilk : i === ICAT ? hlCat : 0;
        op *= 1 - .55 * dim * (1 - hl);
        if (i === IMILK) A(c.r, { fill: mix(C.card, '#E3EEFF', hl), stroke: mix(C.line, C.blue, hl) });
        if (i === ICAT) A(c.r, { fill: mix(C.card, '#FFEBD8', hl), stroke: mix(C.line, TOKC[ICAT], hl) });
      }
      if (i === IHOT) sy = 1 - P(t, tf - .1, .22, Ez.lin);
      TRS(c.g, xs[i], y, s, s * sy); O(c.g, sy < .01 ? 0 : op);
    });
    const pe = P(t, tf + .12, .3, Ez.out);
    TRS(hungry.g, xs[IHOT], Y, 1, pe); O(hungry.g, pe > .01 ? 1 - .55 * dim : 0);
    O(comma, P(t, K('sent', '因为') - .3, .3)); O(period, P(t, K('sent', '了') + .2, .3));

    const qp = P(t, tw + .2, .5, Ez.back) * (1 - P(t, tm - .1, .3));
    pop(bubble, xs[IT], Y - 150, qp);
    O(ring, Wn(t, tw + .2, tm, .4) * .9); A(ring, { cx: xs[IT], cy: Y - 22, transform: `rotate(${((t * 60) % 360).toFixed(1)} ${xs[IT]} ${Y - 22})` });
    DP(arcMilk, P(t, tm, .7) * (1 - P(t, tf - .3, .45))); O(lblMilk, P(t, tm + .5, .4) * (1 - P(t, tf - .4, .4)));
    DP(arcCat, P(t, tcat, .8) * (1 - P(t, tc + .2, .5))); O(lblCat, P(t, tcat + .5, .4) * (1 - P(t, tc + .2, .4)));
    const tctx = K('context', '上下文');
    down.forEach((a, j) => { if (a) DP(a, P(t, tctx - .2 + Math.abs(j - IT) * .08, .8)); });
    const dv = P(t, tctx + .7, .6);
    dots.forEach((d, j) => { if (d) d.update(t, s => arcPt(xs[j], xs[IT], Y + 48, depth(j), s), dv, .45, j * .11); });
    O(ctx, P(t, tctx + .2, .5));
    const tcap = K('context', '注意力机制');
    O(cap, P(t, tcap, .6)); DP(capU, P(t, tcap + .4, .8));
  };
});

/* =========================================================
   场景 2：打分 —— 点积与缩放
   ========================================================= */
scene('qk', svg => {
  const hd = heading(svg, '02', '打分：点积与缩放');
  // 左：“它”与它的 Query
  const it = chip(svg, '它', { fs: 48, fill: '#FFE7B0', stroke: C.gold });
  const R = rng(3);
  const q = vecRow(svg, 120, 380, 8, i => mix('#FFE3DA', C.q, .35 + .6 * R()), { cell: 28 });
  const qLbl = txt(svg, 236, 440, 'q（它的 Query）', { 'text-anchor': 'middle', 'font-size': 28, fill: C.q, 'font-weight': 700 });
  // 中：每个词的 Key 与分数
  const RY = j => 210 + j * 58, KX = 560;
  const band = S('rect', { x: 430, y: RY(IMILK) - 25, width: 450, height: 50, rx: 12, fill: '#E3EEFF', stroke: C.blue, 'stroke-width': 2, opacity: 0 }, svg);
  const keyHead = txt(svg, KX + 88, 170, 'k（每个词的 Key）', { 'text-anchor': 'middle', 'font-size': 26, fill: C.k, 'font-weight': 700 });
  const rows = TOK.map((l, j) => {
    const g = S('g', {}, svg);
    txt(g, KX - 26, RY(j), l, { 'text-anchor': 'end', 'font-size': 30, 'font-weight': 700, fill: j === IT ? '#C9861A' : C.ink });
    vecRow(g, KX, RY(j) - 13, 6, () => mix('#D8F0EC', C.k, .3 + .65 * R()), { cell: 26 });
    const dot = txt(g, KX + 200, RY(j), '·', { 'text-anchor': 'middle', 'font-size': 34, 'font-weight': 900, fill: C.mute });
    const score = txt(g, KX + 262, RY(j), String(RAW[j]), { 'text-anchor': 'middle', 'font-size': 28, 'font-weight': 800, fill: C.q, class: 'math' });
    return { g, dot, score };
  });
  const beam = drawPath(svg, `M300 395 C 400 395, 430 ${RY(IMILK)}, ${KX - 70} ${RY(IMILK)}`, { stroke: C.q, 'stroke-width': 4, 'stroke-dasharray': '1 1' });
  const beamDots = flowDots(svg, 4, C.q, 5);
  // 右：softmax 权重
  const PX = 900, PY = 190, PW = 570, PH = 400;
  const pan = panel(svg, PX, PY, PW, PH);
  const tRaw = txt(svg, PX + 32, PY + 44, 'softmax(原始分数)', { 'font-size': 28, 'font-weight': 800, fill: C.q });
  const tSc = txt(svg, PX + 32, PY + 44, 'softmax(分数 ÷ √d)', { 'font-size': 28, 'font-weight': 800, fill: C.k });
  const demo = txt(svg, PX + PW - 28, PY + 44, '示意数值', { 'text-anchor': 'end', 'font-size': 20, fill: C.mute });
  const chart = bars(svg, { x: PX + 36, y: PY + 90, w: PW - 60, h: 240, labels: TOK, fs: 21, vfs: 22 });
  const warn = txt(svg, PX + PW / 2, PY + PH + 40, '几乎全给了一个词：其余词被忽略，梯度也几乎为 0', { 'text-anchor': 'middle', 'font-size': 26, fill: C.q, 'font-weight': 700 });
  const good = txt(svg, PX + PW / 2, PY + PH + 40, '分布更温和：主次分明，其他词也保留一点', { 'text-anchor': 'middle', 'font-size': 26, fill: C.k, 'font-weight': 700 });
  const badge = S('g', {}, svg);
  S('rect', { x: -118, y: -34, width: 236, height: 68, rx: 34, fill: C.gold }, badge);
  txt(badge, 0, 2, '÷ √d = ÷ 8', { 'text-anchor': 'middle', 'font-size': 32, 'font-weight': 900, fill: '#fff' });
  // 左下：加权求和后的新向量
  const outLbl = txt(svg, 236, 560, '它的新向量 = Σ 权重 × Value', { 'text-anchor': 'middle', 'font-size': 28, fill: C.v, 'font-weight': 700 });
  const out = vecRow(svg, 120, 590, 8, () => C.card, { cell: 28, stroke: C.line });
  const sumDots = TOK.map((_, j) => flowDots(svg, 3, TOKC[j], 6));
  const sumCap = txt(svg, 236, 670, '带上了“牛奶”的信息', { 'text-anchor': 'middle', class: 'hand', 'font-size': 38, fill: '#4F9BE0', 'font-weight': 700 });

  return t => {
    const t0 = T('qk');
    O(hd, P(t, t0 - .5, .6));
    pop(it.g, 236, 300, P(t, K('qk', '它') - .1, .5, Ez.back));
    const tq = K('qk', 'Query');
    q.forEach((c, i) => O(c, P(t, tq - .1 + i * .05, .3)));
    O(qLbl, P(t, tq + .2, .4));
    const tk = K('qk', 'Key');
    O(keyHead, P(t, tk - .2, .4));
    const tdot = K('qk', '点积'), tmatch = K('match', '牛奶');
    const focus = P(t, tmatch, .4);
    rows.forEach((r, j) => {
      O(r.g, P(t, tk - .4 + j * .07, .4) * (1 - .55 * focus * (j !== IMILK)));
      O(r.dot, P(t, tdot + j * .12, .25));
      O(r.score, P(t, tdot + .1 + j * .12, .3));
    });
    O(band, focus);
    DP(beam, P(t, tdot - .2, .7) * (1 - P(t, T('scale'), .5)));
    beamDots.update(t, s => [lerp(300, KX - 70, s), lerp(395, RY(IMILK), Ez.sin(s))], Wn(t, tdot + .4, T('scale'), .4), .6);

    const tscale = T('scale'), tsq = K('sqrt', '除以');
    const panelIn = P(t, tscale - .3, .5);
    O(pan, panelIn); O(chart.g, panelIn); O(demo, P(t, tscale, .5));
    const morph = P(t, tsq + .3, .9);
    O(tRaw, P(t, tscale - .1, .4) * (1 - morph)); O(tSc, morph);
    const grow = P(t, K('scale', '权重') - .3, .8, Ez.out);
    chart.update(W_RAW.map((w, i) => lerp(w, W_SC[i], morph)), grow, {
      color: i => i === IMILK ? mix(C.q, C.k, morph) : mix('#E8B5A8', '#9ED3CC', morph) });
    O(warn, P(t, K('scale', '一个词'), .4) * (1 - P(t, tsq, .3)));
    O(good, P(t, tsq + 1.0, .5));
    pop(badge, PX + PW / 2, PY - 46, P(t, tsq - .1, .5, Ez.back) * (1 - P(t, T('sum') - .2, .4)));

    const tsum = T('sum'), tv = K('sum', 'Value');
    O(outLbl, P(t, tsum, .5));
    out.forEach(c => O(c, P(t, tsum + .2, .5)));
    const fill = P(t, tv + .3, 1.6);
    out.forEach((c, i) => A(c, { fill: mix(C.card, mix('#4F9BE0', C.v, (i % 3) / 6), fill * (.55 + .45 * Math.sin(i * 1.7 + 1) ** 2)), stroke: mix(C.line, '#4F9BE0', fill) }));
    sumDots.forEach((d, j) => d.update(t, s => [lerp(KX - 70, 240, Ez.sin(s)), lerp(RY(j), 604, s)],
      Wn(t, tv, tv + 2.4, .4) * Math.sqrt(W_SC[j]), .5, j * .13));
    O(sumCap, P(t, K('sum', '牛奶'), .5));
  };
});

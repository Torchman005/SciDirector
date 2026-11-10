/* Runs only inside an opaque-origin, offline preview frame. */
(() => {
  'use strict';
  const config = JSON.parse(document.getElementById('scid-editor-preview').textContent);
  const send = value => parent.postMessage({ channel: config.channel, ...value }, '*');
  const fit = () => {
    document.getElementById('film').style.transform = `scale(${Math.min(innerWidth / config.width, innerHeight / config.height)})`;
  };
  new ResizeObserver(fit).observe(document.documentElement);
  fit();
  addEventListener('message', event => {
    if (event.source !== parent || event.data?.channel !== config.channel || event.data?.type !== 'seek') return;
    const { time, seq } = event.data;
    if (!Number.isFinite(time) || time < 0 || time > config.duration || !Number.isInteger(seq)) return;
    try {
      window.__seek(time);
      send({ type: 'frame', seq, time, geometry: window.__fyObjects(), issues: window.__fyLayout() });
    } catch (error) { send({ type: 'error', message: String(error.message || error).slice(0, 500) }); }
  });
  const start = performance.now();
  const wait = () => {
    if (window.__ready) {
      if (window.__errors?.length) send({ type: 'error', message: window.__errors.join('; ').slice(0, 500) });
      else send({ type: 'ready' });
    } else if (performance.now() - start > 10000) send({ type: 'error', message: '预览初始化超时' });
    else setTimeout(wait, 20);
  };
  wait();
})();

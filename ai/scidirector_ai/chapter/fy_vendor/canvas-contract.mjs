/** Project-authored time adapter; no renderer, media, network, player or RAF is started. */
export function createCanvasContract({canvas, width, height, duration, fps=30, supersample=1, draw, resources=Promise.resolve()}) {
  for (const [key, value] of Object.entries({width,height,duration,fps,supersample})) {
    if (!Number.isFinite(value) || value <= 0) throw new RangeError(`${key} must be positive and finite`);
  }
  if (![width,height,supersample].every(Number.isInteger)) throw new RangeError('dimensions and supersample must be integers');
  if (typeof draw !== 'function') throw new TypeError('draw must be a function');
  const ctx=canvas.getContext('2d');
  if (!ctx) throw new Error('Canvas 2D context unavailable');
  canvas.width=width*supersample; canvas.height=height*supersample;
  const film={duration,fps,ready:false,error:null,seek};
  film.readyPromise=Promise.resolve(resources).then(()=>{film.ready=true;}, error=>{
    film.error=error instanceof Error ? error : new Error(String(error));
    throw film.error;
  });
  function seek(seconds) {
    if (film.error) throw film.error;
    if (!film.ready) throw new Error('Resources not ready; await readyPromise');
    if (!Number.isFinite(seconds)) throw new TypeError('seconds must be finite');
    const t=Math.max(0,Math.min(duration,seconds));
    if (typeof ctx.reset === 'function') ctx.reset(); else canvas.width=width*supersample;
    ctx.setTransform(supersample,0,0,supersample,0,0);
    ctx.save();
    try { draw({ctx,t,frame:Math.round(t*fps),width,height,fps,supersample}); }
    finally { ctx.restore(); }
    return t;
  }
  return film;
}

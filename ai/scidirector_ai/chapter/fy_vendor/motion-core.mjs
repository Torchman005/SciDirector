/** Pure numeric helpers. No DOM, network, media, wall clock or hidden simulation state. */
const finite = (x, label='value') => { if (!Number.isFinite(x)) throw new Error(`${label} must be finite`); return x; };
const positive = (x, label) => { finite(x,label); if(x<=0)throw new Error(`${label} must be positive`); return x; };
export const clamp = (x,a=0,b=1) => { finite(x); if(a>b)throw new Error('reversed clamp'); return Math.min(b,Math.max(a,x)); };
export const lerp = (a,b,p) => a+(b-a)*p;
export const progress = (t,start,end) => { [t,start,end].forEach(x=>finite(x)); if(end<=start)throw new Error('empty time range'); return clamp((t-start)/(end-start)); };
export const secondsToFrame = (seconds,fps,mode='nearest') => {
  finite(seconds); positive(fps,'fps');
  const f={nearest:Math.round,start:Math.floor,end:Math.ceil}[mode];
  if(!f)throw new Error('rounding mode'); return f(seconds*fps);
};
export const frameToSeconds = (frame,fps) => finite(frame)/positive(fps,'fps');
export const steppedTime = (t,drawingsPerSecond=12) => Math.floor(finite(t)*positive(drawingsPerSecond,'drawing rate'))/drawingsPerSecond;
export const loopPhase = (t,duration) => {positive(duration,'duration');return ((finite(t)%duration)+duration)%duration/duration;};
export const easeOutCubic = p => 1-(1-clamp(p))**3;
export const easeInOut = p => {p=clamp(p);return p*p*(3-2*p);};
export function cubicBezier(x1,y1,x2,y2){
  [x1,y1,x2,y2].forEach(x=>finite(x)); if(x1<0||x1>1||x2<0||x2>1)throw new Error('Bezier x controls outside [0,1]');
  const q=(u,a,b)=>3*(1-u)**2*u*a+3*(1-u)*u*u*b+u*u*u;
  return p=>{p=clamp(p);if(p===0||p===1)return p;let lo=0,hi=1;for(let n=0;n<48;n++){const u=(lo+hi)/2;if(q(u,x1,x2)<p)lo=u;else hi=u;}return q((lo+hi)/2,y1,y2);};
}
/** Unit step x(0)=0, x'(0)=velocity, target 1. coefficient damping c, stiffness k, mass m. */
export function springAt(t,{stiffness=140,damping=20,mass=1,velocity=0}={}){
  finite(t);positive(stiffness,'stiffness');positive(mass,'mass');finite(damping);finite(velocity);
  if(damping<0)throw new Error('negative damping'); if(t<=0)return 0;
  const w=Math.sqrt(stiffness/mass), z=damping/(2*Math.sqrt(stiffness*mass));
  if(Math.abs(z-1)<1e-7)return 1+(-1+(velocity-w)*t)*Math.exp(-w*t);
  if(z<1){const wd=w*Math.sqrt(1-z*z);return 1+Math.exp(-z*w*t)*(-Math.cos(wd*t)+(velocity-z*w)/wd*Math.sin(wd*t));}
  const r1=-w*(z-Math.sqrt(z*z-1)),r2=-w*(z+Math.sqrt(z*z-1));
  const a=(velocity+r2)/(r1-r2),b=-1-a;return 1+a*Math.exp(r1*t)+b*Math.exp(r2*t);
}
/** Stable per-key pseudo random; no call-order dependency. Not cryptographic. */
export function keyedRandom(seed,key){let h=2166136261;for(const c of `${seed}:${key}`){h^=c.codePointAt(0);h=Math.imul(h,16777619);}h^=h>>>16;h=Math.imul(h,0x7feb352d);h^=h>>>15;h=Math.imul(h,0x846ca68b);h^=h>>>16;return (h>>>0)/4294967296;}
export function keyed(t,keys,{ease=easeInOut,log=false}={}){
  finite(t);if(keys.length<1)throw new Error('empty keys');
  keys.forEach(([at,v],i)=>{finite(at);finite(v);if(i&&at<=keys[i-1][0])throw new Error('keys must increase');if(log&&v<=0)throw new Error('log values must be positive');});
  if(t<=keys[0][0])return keys[0][1];if(t>=keys.at(-1)[0])return keys.at(-1)[1];
  const i=keys.findIndex(k=>k[0]>t),a=keys[i-1],b=keys[i],p=ease(progress(t,a[0],b[0]));
  return log?Math.exp(lerp(Math.log(a[1]),Math.log(b[1]),p)):lerp(a[1],b[1],p);
}
export function camera2D(point,{center=[0,0],screen=[0,0],zoom=1,rotation=0}={}){
  positive(zoom,'zoom');const x=point[0]-center[0],y=point[1]-center[1],c=Math.cos(rotation),s=Math.sin(rotation);
  return [screen[0]+zoom*(c*x-s*y),screen[1]+zoom*(s*x+c*y)];
}
export function cameraCenterForAnchor(anchor,screenTarget,{screen=[0,0],zoom=1,rotation=0}={}){
  positive(zoom,'zoom');const x=(screenTarget[0]-screen[0])/zoom,y=(screenTarget[1]-screen[1])/zoom,c=Math.cos(rotation),s=Math.sin(rotation);
  return [anchor[0]-(c*x+s*y),anchor[1]-(-s*x+c*y)];
}
/** Output source rectangle for cover; focal point is normalized; no image editing. */
export function coverCrop(iw,ih,bw,bh,focus=[.5,.5],zoom=1){
  [iw,ih,bw,bh,zoom].forEach(x=>positive(x,'dimension/zoom'));if(zoom<1)throw new Error('cover zoom below 1');
  const scale=Math.max(bw/iw,bh/ih)*zoom,sw=bw/scale,sh=bh/scale;
  return {x:clamp(focus[0]*iw-sw/2,0,iw-sw),y:clamp(focus[1]*ih-sh/2,0,ih-sh),width:sw,height:sh,scale};
}
export function visibleBoxFraction(subject,crop){
  positive(subject.width,'subject width');positive(subject.height,'subject height');
  const w=Math.max(0,Math.min(subject.x+subject.width,crop.x+crop.width)-Math.max(subject.x,crop.x));
  const h=Math.max(0,Math.min(subject.y+subject.height,crop.y+crop.height)-Math.max(subject.y,crop.y));
  return w*h/(subject.width*subject.height);
}
export function pointAlongPolyline(points,p){
  if(points.length<2)throw new Error('path needs two points');p=clamp(p);
  const lens=points.slice(1).map((b,i)=>Math.hypot(b[0]-points[i][0],b[1]-points[i][1]));
  let d=p*lens.reduce((a,b)=>a+b,0);for(let i=0;i<lens.length;i++){if(d<=lens[i]&&lens[i]>0)return [lerp(points[i][0],points[i+1][0],d/lens[i]),lerp(points[i][1],points[i+1][1],d/lens[i])];d-=lens[i];}return [...points.at(-1)];
}
/** Exact endpoint wave: no discontinuous nonzero cosine at impact. */
export const contactRing = (t,contact,{amplitude=8,decay=7,frequency=38}={}) => t<contact?0:amplitude*Math.exp(-decay*(t-contact))*Math.sin(frequency*(t-contact));
export function formationPoint(t,element,group){
  const tau=t-element.delay,p=group.path(tau),a=group.heading(tau),s=group.scale(tau),[x,y]=element.offset;
  const envelope=group.flutterEnvelope(tau),dy=(group.wave||0)*Math.sin((group.k||0)*x-(group.omega||0)*t);
  const flutter=envelope*(Math.sin(t*element.frequency+element.phase));
  return [p[0]+s*(Math.cos(a)*x-Math.sin(a)*(y+dy)),p[1]+s*(Math.sin(a)*x+Math.cos(a)*(y+dy))+flutter];
}

/* Trusted renderer. Each seek computes the complete state from immutable IR. */
(() => {
  'use strict';
  const config = JSON.parse(document.getElementById('scid-scene-v1').textContent);
  const {scene, width:W, height:H, duration:D, style} = config;
  const animatedUntil=config.animatedUntil || 1;
  const stage = document.getElementById('stage');
  const light = style.theme === 'light';
  const palette = {primary:style.primary, text:light?'#101216':'#E6ECFF', muted:light?'#485368':'#A8B5CF'};
  const color = value => palette[value] || value;
  document.body.style.backgroundColor = style.background;
  if(style.backgroundCSS) document.body.style.cssText=style.backgroundCSS;
  // Draft capture uses a smaller viewport but the same authored layout/fonts.
  Object.assign(stage.style,{width:W+'px',height:H+'px',transformOrigin:'top left',
    transform:`scale(${innerWidth/W},${innerHeight/H})`});
  stage.style.fontFamily = JSON.stringify(style.font)+',"Microsoft YaHei",sans-serif';
  const make = (tag,parent,cls) => {
    const node=document.createElement(tag);
    if(cls) node.className=cls;
    parent.appendChild(node); return node;
  };
  const svgNode = (tag,parent,attrs) => {
    const node=document.createElementNS('http://www.w3.org/2000/svg',tag);
    for(const [key,value] of Object.entries(attrs)) node.setAttribute(key,String(value));
    parent.appendChild(node);return node;
  };
  const defaults = {opacity:1,dx:0,dy:0,rotation:0,reveal:1,scale:1};
  const sample = (frames,t) => {
    if(!frames.length) return {...defaults};
    let a=frames[0];
    if(t<=a.time) return {...defaults,...a};
    for(let i=1;i<frames.length;i++) {
      const b=frames[i];
      if(t<b.time) {
        let p=(t-a.time)/(b.time-a.time);
        if(b.easing==='step') p=0;
        else if(b.easing==='smooth') p=p*p*(3-2*p);
        else if(b.easing==='ease_in') p=p*p*p;
        else if(b.easing==='ease_out') p=1-Math.pow(1-p,3);
        else if(b.easing==='spring') p=(1-(1+7*p)*Math.exp(-7*p))/(1-8*Math.exp(-7));
        const result={expression:a.expression,gesture:a.gesture};
        for(const key of Object.keys(defaults)) result[key]=a[key]+(b[key]-a[key])*p;
        return result;
      }
      a=b;
    }
    return {...defaults,...a};
  };
  const nodes=scene.elements.map(el => {
    const root=make('div',stage,'scene-element'); root.id=el.id;
    const b=el.box;
    Object.assign(root.style,{left:b.x*W+'px',top:b.y*H+'px',width:b.width*W+'px',height:b.height*H+'px',
      fontSize:el.font_size+'px',textAlign:el.align,color:color(el.color)});
    const record={el,root,content:null,bars:[],stroke:null,strokeLength:0,arrow:null,character:null};
    if(['text','card','code'].includes(el.kind)) {
      root.className+=' scene-text'+(el.kind==='card'?' scene-card':el.kind==='code'?' scene-code':'');
      if(el.kind!=='text') {
        root.style.borderColor=style.primary;
        root.style.backgroundColor=light?'#F0F3FA':'#151E32';
      }
      record.content=make('span',root);record.content.textContent=el.text;
    } else if(el.kind==='bars') {
      const max=Math.max(...el.data.map(d=>d.value)) || 1;
      for(const d of el.data) {
        const row=make('div',root,'scene-bar-row');row.style.height=(100/el.data.length)+'%';
        const label=make('div',row,'scene-bar-label');
        make('span',label).textContent=d.label;
        make('span',label).textContent=String(d.value)+' '+el.unit;
        const bar=make('div',row,'scene-bar-fill');
        bar.style.width=(100*d.value/max)+'%';bar.style.backgroundColor=color(el.color);
        record.bars.push(bar);
      }
    } else if(el.kind==='character') {
      record.character=createScienceCharacter(root,svgNode,color(el.color==='text'?'primary':el.color));
    } else {
      const w=b.width*W,h=b.height*H;
      const svg=svgNode('svg',root,{viewBox:`0 0 ${w} ${h}`});
      if(el.kind==='rect') svgNode('rect',svg,{x:2,y:2,width:Math.max(1,w-4),height:Math.max(1,h-4),rx:16,fill:color(el.color)});
      if(el.kind==='circle') svgNode('circle',svg,{cx:w/2,cy:h/2,r:Math.max(1,Math.min(w,h)/2-2),fill:color(el.color)});
      if(['line','polyline'].includes(el.kind)) {
        const attrs={stroke:color(el.color),'stroke-width':el.stroke_width,fill:'none',pathLength:1,'stroke-linecap':'round'};
        if(el.kind==='line') Object.assign(attrs,{x1:4,y1:4,x2:Math.max(4,w-8),y2:Math.max(4,h-8)});
        else attrs.points=el.points.map(p=>`${p.x*w},${p.y*h}`).join(' ');
        record.stroke=svgNode(el.kind==='line'?'line':'polyline',svg,attrs);
        record.strokeLength=record.stroke.getTotalLength();
        if(el.arrow) {
          record.arrow=svgNode('path',svg,{d:'M-10,-4.5 L0,0 L-10,4.5 Z',fill:color(el.color),'data-arrow':el.id});
        }
      }
    }
    return record;
  });
  window.__seek = seconds => {
    const t=Math.max(0,Math.min(D,Number.isFinite(seconds)?seconds:0))/D;
    for(const {el,root,content,bars,stroke,strokeLength,arrow,character} of nodes) {
      const last=el.keyframes.length ? el.keyframes[el.keyframes.length-1].time : 0;
      const local=last>animatedUntil ? Math.min(1,t/animatedUntil) : t;
      const s=sample(el.keyframes, local);
      root.style.opacity=String(s.opacity);
      root.style.transform=`translate(${s.dx*W}px,${s.dy*H}px) rotate(${s.rotation}deg) scale(${s.scale})`;
      if(character) {
        let index=0;
        for(let i=1;i<el.keyframes.length;i++) if(local>=el.keyframes[i].time) index=i;
        const frame=el.keyframes[index];
        s.gestureFrom=index>0?el.keyframes[index-1].gesture:s.gesture;
        s.gestureMix=frame?Math.min(1,Math.max(0,(local-frame.time)*D/.3)):1;
        character(t*D,s);
      }
      if(stroke) {
        stroke.style.visibility=s.reveal>0?'visible':'hidden';
        stroke.setAttribute('stroke-dasharray','1');stroke.setAttribute('stroke-dashoffset',String(1-s.reveal));
        if(arrow) {
          arrow.style.visibility=s.reveal>0?'visible':'hidden';
          const length=strokeLength*s.reveal, tip=stroke.getPointAtLength(length);
          const behind=stroke.getPointAtLength(Math.max(0,length-.1));
          const angle=Math.atan2(tip.y-behind.y,tip.x-behind.x)*180/Math.PI;
          arrow.setAttribute('transform',`translate(${tip.x} ${tip.y}) rotate(${angle})`);
        }
      }
      if(el.kind==='code') content.textContent=Array.from(el.text).slice(0,Math.floor(Array.from(el.text).length*s.reveal)).join('');
      for(const bar of bars) bar.style.transform=`scaleX(${s.reveal})`;
    }
  };
  window.__seek(0);window.__ready=true;
})();

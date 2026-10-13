/* Trusted renderer. Each seek computes the complete state from immutable IR. */
(() => {
  'use strict';
  const config = JSON.parse(document.getElementById('scid-scene-v1').textContent);
  const {scene, width:W, height:H, duration:D, style} = config;
  const imageData=config.imageData || {};
  const imageRoles=config.imageRoles || {};
  window.__ready=false;
  window.__loadError='';
  const animatedUntil=config.animatedUntil || 1;
  // A shared timeline preserves synchronization between connected objects.
  const timelineEnd=(scene.elements.some(e=>e.keyframes.some(k=>k.time>animatedUntil)) ||
    (scene.motion_groups||[]).some(g=>g.keyframes.some(k=>k.time>animatedUntil)))?animatedUntil:1;
  const stage = document.getElementById('stage');
  const paper=Boolean(style.paperStage);
  if(paper) stage.classList.add('paper-stage');
  const light = style.theme === 'light';
  const palette = paper
    ? {primary:style.primary,text:'#382F2A',muted:'#71645C',secondary:'#527C83',accent:'#957399',success:'#5A8068',warning:'#B47B3D'}
    : {primary:style.primary, text:light?'#101216':'#E6ECFF', muted:light?'#485368':'#A8B5CF',
      secondary:light?'#087F8C':'#59DCE4',accent:light?'#7954BF':'#BB9CFF',
      success:light?'#167753':'#7EE3AF',warning:light?'#B14A24':'#FFB487'};
  const color = value => palette[value] || value;
  const mix=(a,b,t)=>{
    const rgb=h=>{h=h.slice(1);if(h.length===3)h=h.split('').map(c=>c+c).join('');return [0,2,4].map(i=>parseInt(h.slice(i,i+2),16));};
    const x=rgb(a),y=rgb(b);return '#'+x.map((v,i)=>Math.round(v+(y[i]-v)*t).toString(16).padStart(2,'0')).join('');
  };
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
    if(t<a.time) return {...defaults,...a,opacity:0};
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
  const groups=new Map((scene.motion_groups||[]).map(group=>{
    const members=scene.elements.filter(el=>el.motion_group===group.id);
    const left=Math.min(...members.map(el=>el.box.x)),top=Math.min(...members.map(el=>el.box.y));
    const right=Math.max(...members.map(el=>el.box.x+el.box.width));
    const bottom=Math.max(...members.map(el=>el.box.y+el.box.height));
    return [group.id,{cx:(left+right)*W/2,cy:(top+bottom)*H/2,
      frames:group.keyframes.map(frame=>({...defaults,...frame}))}];
  }));
  const imageLoads=[];
  const nodes=scene.elements.map(el => {
    const root=make('div',stage,'scene-element'); root.id=el.id;
    root.setAttribute('data-scene-kind',el.kind);
    root.setAttribute('data-overlap-group',el.overlap_group || '');
    root.setAttribute('data-decorative',String(Boolean(el.decorative)));
    if(el.anchor_from){
      root.setAttribute('data-anchor-from',el.anchor_from);
      root.setAttribute('data-anchor-to',el.anchor_to);
    }
    const material=el.material==='auto'?(style.animationStyle==='anime'?'gradient':'solid'):el.material;
    root.setAttribute('data-material',material);
    root.setAttribute('data-fill-color',color(el.color));
    const b=el.box;
    Object.assign(root.style,{left:b.x*W+'px',top:b.y*H+'px',width:b.width*W+'px',height:b.height*H+'px',
      fontSize:el.font_size+'px',textAlign:el.align,color:color(el.color),zIndex:String(el.layer)});
    if(el.anchor_from) Object.assign(root.style,{left:'0px',top:'0px',width:W+'px',height:H+'px',pointerEvents:'none'});
    const record={el,root,content:null,bars:[],cells:[],packets:[],stroke:null,strokeLength:0,arrow:null,character:null,images:null,iconAction:null};
    if(['text','card','code'].includes(el.kind)) {
      root.className+=' scene-text'+(el.kind==='card'?' scene-card':el.kind==='code'?' scene-code':'');
      if(el.kind!=='text') {
        const tint=color(el.color==='text'?'primary':el.color),base=paper?'#FFF9EF':light?'#F0F3FA':'#151E32';
        root.style.borderColor=el.stroke_color?color(el.stroke_color):mix(tint,base,.5);
        root.style.borderRadius=el.corner_radius+'px';
        root.style.backgroundColor=material==='outline'?'transparent':base;
        if(['gradient','glass'].includes(material)){
          root.style.backgroundImage=`linear-gradient(135deg,${mix(base,tint,.16)},${base} 70%)`;
          root.style.boxShadow=`inset 0 1px 0 ${mix(base,'#FFFFFF',.2)},0 10px 28px #00000020`;
        }
      }
      record.content=make('span',root);record.content.textContent=el.text;
    } else if(el.kind==='image') {
      root.setAttribute('data-scene-asset-role',imageRoles[el.asset_id]||'prop');
      record.images=new Map();
      const ids=[...new Set([el.asset_id,...el.keyframes.map(frame=>frame.asset_id).filter(Boolean)])];
      for(const id of ids){
        const src=imageData[id];
        if(typeof src!=='string' || !src.startsWith('data:image/')) throw new Error(`图片素材 ${id} 没有可用数据`);
        const img=make('img',root,'scene-image');
        img.alt=''; img.draggable=false; img.dataset.assetId=id;
        Object.assign(img.style,{position:'absolute',left:'0',top:'0',width:'100%',height:'100%',objectFit:el.image_fit||'contain',visibility:'hidden',opacity:'0'});
        record.images.set(id,img);
        imageLoads.push(new Promise((resolve,reject)=>{
          img.onload=()=>{ if(img.naturalWidth&&img.naturalHeight) resolve(); else reject(new Error(`图片素材 ${id} 解码为空`)); };
          img.onerror=()=>reject(new Error(`图片素材 ${id} 解码失败`));
          img.src=src;
          if(img.complete) {
            if(img.naturalWidth&&img.naturalHeight) resolve();
            else reject(new Error(`图片素材 ${id} 解码失败`));
          }
        }));
      }
    } else if(el.kind==='grid') {
      const columns=Math.min(el.columns,el.cells.length);
      Object.assign(root.style,{display:'grid',gridTemplateColumns:`repeat(${columns},1fr)`,gridTemplateRows:`repeat(${Math.ceil(el.cells.length/columns)},1fr)`,gap:'10px'});
      for(const item of el.cells){
        const cell=make('div',root,'scene-grid-cell'),tint=color(item.color||el.color),base=paper?'#FFF9EF':light?'#F0F3FA':'#151E32';
        Object.assign(cell.style,{display:'flex',alignItems:'center',justifyContent:'center',border:`2px solid ${mix(tint,base,.2)}`,borderRadius:'8px',
          color:palette.text,background:`linear-gradient(145deg,${mix(base,tint,.36)},${mix(base,tint,.12)})`,boxShadow:`inset 0 2px 0 ${tint}30,0 4px 0 ${base}`,overflow:'hidden'});
        cell.textContent=item.label;record.cells.push(cell);
      }
    } else if(el.kind==='bars') {
      const max=Math.max(...el.data.map(d=>d.value)) || 1;
      for(const d of el.data) {
        const row=make('div',root,'scene-bar-row');row.style.height=(100/el.data.length)+'%';
        const label=make('div',row,'scene-bar-label');
        make('span',label).textContent=d.label;
        make('span',label).textContent=String(d.value)+' '+el.unit;
        const bar=make('div',row,'scene-bar-fill');
        const tint=color(d.color||el.color);
        bar.style.width=(100*d.value/max)+'%';bar.style.backgroundColor=tint;
        if(material!=='solid')bar.style.backgroundImage=`linear-gradient(90deg,${mix(tint,'#000000',.25)},${tint})`;
        record.bars.push(bar);
      }
    } else if(el.kind==='character') {
      record.character=createScienceCharacter(root,svgNode,color(el.color==='text'?'primary':el.color));
    } else {
      const w=el.anchor_from?W:b.width*W,h=el.anchor_from?H:b.height*H;
      const svg=svgNode('svg',root,{viewBox:`0 0 ${w} ${h}`});
      const tint=color(el.color),outlined=material==='outline';
      if(el.kind==='icon')record.iconAction=createScienceIcon(svg,svgNode,el.glyph,w,h,tint,
        paper?'#FFF9EF':light?'#F0F3FA':'#151E32');
      let fill=outlined?'none':tint;
      const stroke=el.stroke_color?color(el.stroke_color):material==='solid'?'none':mix(tint,light?'#000000':'#FFFFFF',.35);
      if(['gradient','glass'].includes(material)&&['rect','circle'].includes(el.kind)){
        const defs=svgNode('defs',svg,{}),id='scid-gradient-'+el.id;
        const gradient=svgNode('linearGradient',defs,{id,x1:0,y1:0,x2:1,y2:1});
        svgNode('stop',gradient,{offset:'0%','stop-color':mix(tint,material==='glass'?style.background:'#FFFFFF',material==='glass'?.68:.15)});
        svgNode('stop',gradient,{offset:'100%','stop-color':mix(tint,material==='glass'?style.background:'#000000',material==='glass'?.92:.35)});
        fill=`url(#${id})`;
      }
      const sw=stroke==='none'?0:el.stroke_width, inset=Math.max(2,sw/2);
      if(el.kind==='rect') svgNode('rect',svg,{x:inset,y:inset,width:Math.max(1,w-inset*2),height:Math.max(1,h-inset*2),rx:el.corner_radius,fill,stroke,'stroke-width':sw});
      if(el.kind==='circle') svgNode('circle',svg,{cx:w/2,cy:h/2,r:Math.max(1,Math.min(w,h)/2-inset),fill,stroke,'stroke-width':sw});
      if(['line','polyline'].includes(el.kind)) {
        const attrs={stroke:color(el.color),'stroke-width':el.stroke_width,fill:'none',pathLength:1,'stroke-linecap':'round','stroke-linejoin':'round'};
        if(el.kind==='line') Object.assign(attrs,el.anchor_from?{x1:0,y1:0,x2:0,y2:0}:
          {x1:4,y1:4,x2:Math.max(4,w-8),y2:Math.max(4,h-8)});
        else attrs.points=el.points.map(p=>`${p.x*w},${p.y*h}`).join(' ');
        record.stroke=svgNode(el.kind==='line'?'line':'polyline',svg,attrs);
        record.strokeLength=record.stroke.getTotalLength();
        if(el.flow){
          for(let i=0;i<3;i++)record.packets.push(svgNode('circle',svg,{r:Math.max(3,el.stroke_width*1.15),fill:tint,stroke:light?'#FFFFFF':'#E6ECFF','stroke-width':1}));
        }
        if(el.arrow) {
          record.arrow=svgNode('path',svg,{d:'M-10,-4.5 L0,0 L-10,4.5 Z',fill:color(el.color),'data-arrow':el.id});
        }
      }
    }
    return record;
  });
  const byId=new Map(nodes.map(record=>[record.el.id,record]));
  const paintStroke=(record,s,t)=>{
    const {el,stroke,packets,arrow}=record;
    const strokeLength=stroke.getTotalLength();
    stroke.style.visibility=s.reveal>0?'visible':'hidden';
    stroke.setAttribute('stroke-dasharray','1');stroke.setAttribute('stroke-dashoffset',String(1-s.reveal));
    for(let i=0;i<packets.length;i++){
      const fraction=(Math.min(t,timelineEnd)*D*.55+i/packets.length)%1;
      const point=stroke.getPointAtLength(strokeLength*fraction);
      packets[i].setAttribute('cx',point.x);packets[i].setAttribute('cy',point.y);
      packets[i].style.visibility=fraction<s.reveal?'visible':'hidden';
    }
    if(arrow) {
      arrow.style.visibility=s.reveal>0?'visible':'hidden';
      const length=strokeLength*s.reveal, tip=stroke.getPointAtLength(length);
      const behind=stroke.getPointAtLength(Math.max(0,length-.1));
      const angle=Math.atan2(tip.y-behind.y,tip.x-behind.x)*180/Math.PI;
      arrow.setAttribute('transform',`translate(${tip.x} ${tip.y}) rotate(${angle})`);
    }
  };
  const connectionPoint=(rect,toward)=>{
    const cx=(rect.left+rect.right)/2,cy=(rect.top+rect.bottom)/2;
    const dx=toward.x-cx,dy=toward.y-cy;
    const factor=Math.min((rect.width/2)/Math.max(Math.abs(dx),.001),
      (rect.height/2)/Math.max(Math.abs(dy),.001));
    return {x:cx+dx*factor,y:cy+dy*factor};
  };
  window.__seek = seconds => {
    const t=Math.max(0,Math.min(D,Number.isFinite(seconds)?seconds:0))/D;
    const local=Math.min(1,t/timelineEnd);
    for(const {el,root,content,bars,cells,packets,stroke,strokeLength,arrow,character,images,iconAction} of nodes) {
      const s=sample(el.keyframes, local);
      const group=groups.get(el.motion_group);
      if(group){
        const motion=sample(group.frames,local),cx=(el.box.x+el.box.width/2)*W,cy=(el.box.y+el.box.height/2)*H;
        const dx=motion.dx*W+(motion.scale-1)*(cx-group.cx)+motion.scale*s.dx*W;
        const dy=motion.dy*H+(motion.scale-1)*(cy-group.cy)+motion.scale*s.dy*H;
        root.style.opacity=String(s.opacity*motion.opacity);
        root.style.transform=`translate(${dx}px,${dy}px) rotate(${s.rotation}deg) scale(${s.scale*motion.scale})`;
      }else{
        root.style.opacity=String(s.opacity);
        root.style.transform=`translate(${s.dx*W}px,${s.dy*H}px) rotate(${s.rotation}deg) scale(${s.scale})`;
      }
      if(images){
        let current=el.asset_id,previous=current,switchedAt=null;
        for(const frame of el.keyframes){
          if(local<frame.time) break;
          if(frame.asset_id && frame.asset_id!==current){previous=current;current=frame.asset_id;switchedAt=frame.time;}
        }
        const blend=switchedAt===null?1:Math.max(0,Math.min(1,(local-switchedAt)*D/.18));
        for(const [id,img] of images){
          const opacity=id===current?blend:(id===previous?1-blend:0);
          img.style.opacity=String(opacity);
          img.style.visibility=opacity>0?'visible':'hidden';
        }
      }
      if(character) {
        let index=0;
        for(let i=1;i<el.keyframes.length;i++) if(local>=el.keyframes[i].time) index=i;
        const frame=el.keyframes[index];
        s.gestureFrom=index>0?el.keyframes[index-1].gesture:s.gesture;
        s.gestureMix=frame?Math.min(1,Math.max(0,(local-frame.time)*D/.3)):1;
        character(t*D,s);
      }
      if(iconAction) iconAction(s.reveal);
      if(stroke && !el.anchor_from) paintStroke(byId.get(el.id),s,t);
      if(el.kind==='code') content.textContent=Array.from(el.text).slice(0,Math.floor(Array.from(el.text).length*s.reveal)).join('');
      for(const bar of bars) bar.style.transform=`scaleX(${s.reveal})`;
      for(let i=0;i<cells.length;i++){
        const progress=Math.max(0,Math.min(1,s.reveal*cells.length-i));
        cells[i].style.opacity=String(progress);cells[i].style.transform=`translateY(${(1-progress)*12}px)`;
      }
    }
    // Resolve connectors after all objects have reached their absolute-time
    // positions. A connector is never animated by guessed local coordinates.
    const stageRect=nodes.some(record=>record.el.anchor_from)?stage.getBoundingClientRect():null;
    for(const record of nodes){
      const {el,root,stroke}=record;
      if(!el.anchor_from) continue;
      const source=byId.get(el.anchor_from).root,target=byId.get(el.anchor_to).root;
      const sourceRect=source.getBoundingClientRect(),targetRect=target.getBoundingClientRect();
      const aCenter={x:(sourceRect.left+sourceRect.right)/2,y:(sourceRect.top+sourceRect.bottom)/2};
      const bCenter={x:(targetRect.left+targetRect.right)/2,y:(targetRect.top+targetRect.bottom)/2};
      const a=connectionPoint(sourceRect,bCenter),b=connectionPoint(targetRect,aCenter);
      stroke.setAttribute('x1',(a.x-stageRect.left)*W/stageRect.width);
      stroke.setAttribute('y1',(a.y-stageRect.top)*H/stageRect.height);
      stroke.setAttribute('x2',(b.x-stageRect.left)*W/stageRect.width);
      stroke.setAttribute('y2',(b.y-stageRect.top)*H/stageRect.height);
      const endpointsVisible=Number(getComputedStyle(source).opacity)>.1 && Number(getComputedStyle(target).opacity)>.1;
      root.style.opacity=endpointsVisible?String(sample(el.keyframes,local).opacity):'0';
      paintStroke(record,sample(el.keyframes,local),t);
    }
  };
  window.__seek(0);
  Promise.all(imageLoads).then(()=>{window.__ready=true;}).catch(error=>{
    window.__loadError=String(error);
    window.__ready=false;
  });
})();

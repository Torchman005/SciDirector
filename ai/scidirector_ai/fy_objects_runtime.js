/* Data owns geometry; registered Canvas painters own the artwork inside it. */
(()=>{
'use strict';
window.SCIDFyObjects=Object.freeze({create({D,ctx,C,W,H,fail,setOwner}) {
  const doc=D.object_document, painters=new Map(), routeCache=new Map();
  let states=[], edges=[];
  const visible=s=>s.opacity>.15;
  const overlap=(a,b)=>Math.min(a.right,b.right)-Math.max(a.left,b.left)>3&&
    Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>3;
  const sample=(frames,t)=>{
    const identity={dx:0,dy:0,scale:1,opacity:1};
    if(!frames.length)return identity;
    if(t<frames[0].time)return {...frames[0],opacity:0};
    let a=frames[0],b=a;
    for(const frame of frames){if(frame.time<=t)a=frame;else{b=frame;break;}b=a;}
    let p=a===b?1:Math.min(1,Math.max(0,(t-a.time)/(b.time-a.time)));
    if(b.easing==='smooth')p=p*p*(3-2*p);
    return Object.fromEntries(['dx','dy','scale','opacity'].map(k=>[k,a[k]+(b[k]-a[k])*p]));
  };
  function register(id,draw) {
    if(!doc||D.first_ids.length!==1||window.__sceneTag!==D.first_ids[0]||
       !doc.objects.some(o=>o.id===id)||painters.has(id)||typeof draw!=='function')fail(`对象注册无效：${id}`);
    painters.set(id,draw);
  }
  function check() {
    for(const o of doc?.objects||[])if(!painters.has(o.id))fail(`对象未注册：${o.id}`);
  }
  function render(t,api,textBoxes) {
    states=[];edges=[];
    if(!doc)return;
    states=doc.objects.map(o=>{
      const m=sample(o.keyframes,t/D.timing.dur),b=o.box;
      const w=b.width*W,h=b.height*H,cx=(b.x+b.width/2+m.dx)*W,cy=(b.y+b.height/2+m.dy)*H;
      return {id:o.id,object:o,w,h,scale:m.scale,opacity:m.opacity,
        rect:{left:cx-w*m.scale/2,top:cy-h*m.scale/2,right:cx+w*m.scale/2,bottom:cy+h*m.scale/2}};
    });
    const byId=new Map(states.map(s=>[s.id,s]));
    const obstacles=states.filter(visible).map(s=>({id:s.id,rect:s.rect}));
    for(const [i,b] of textBoxes.entries())obstacles.push({id:'$text'+i,rect:{left:b.x,top:b.y,right:b.right,bottom:b.bottom}});
    const a=D.presenter_area;
    if(a)obstacles.push({id:'$presenter',rect:{left:a.x*W,top:a.y*H,right:(a.x+a.width)*W,bottom:(a.y+a.height)*H}});
    const geometryKey=JSON.stringify(obstacles);
    // Cache only identical geometry, including every obstacle, never the last seek.
    for(const edge of doc.relations){
      const source=byId.get(edge.source),target=byId.get(edge.target);
      const shown=visible(source)&&visible(target);
      const key=edge.id+'|'+geometryKey;
      if(shown&&!routeCache.has(key)){
        if(routeCache.size>=512)routeCache.clear();
        routeCache.set(key,SCIDRouting.route(source,target,obstacles,{width:W,height:H*.85,
          sourcePort:edge.source_port,targetPort:edge.target_port}));
      }
      const points=shown?routeCache.get(key):null;
      edges.push({...edge,status:!shown?'hidden':points?'routed':'blocked',points:points||[]});
      if(!points)continue;
      ctx.save();ctx.beginPath();ctx.globalAlpha=Math.min(source.opacity,target.opacity);
      ctx.strokeStyle=C[edge.color];ctx.fillStyle=C[edge.color];ctx.lineWidth=edge.stroke_width;
      ctx.lineJoin='round';ctx.moveTo(points[0].x,points[0].y);
      for(const p of points.slice(1))ctx.lineTo(p.x,p.y);
      if(edge.flow){ctx.setLineDash([12,10]);ctx.lineDashOffset=-t*36;}
      ctx.stroke();ctx.setLineDash([]);
      if(edge.arrow){
        const end=points[points.length-1],prev=points[points.length-2],angle=Math.atan2(end.y-prev.y,end.x-prev.x);
        const len=Math.max(10,edge.stroke_width*3);
        ctx.beginPath();ctx.moveTo(end.x,end.y);
        ctx.lineTo(end.x-len*Math.cos(angle-.45),end.y-len*Math.sin(angle-.45));
        ctx.lineTo(end.x-len*Math.cos(angle+.45),end.y-len*Math.sin(angle+.45));ctx.closePath();ctx.fill();
      }
      ctx.restore();
    }
    for(const s of [...states].sort((a,b)=>a.object.layer-b.object.layer)){
      if(s.opacity<=0)continue;
      ctx.save();ctx.globalAlpha=s.opacity;ctx.translate(s.rect.left,s.rect.top);ctx.scale(s.scale,s.scale);
      ctx.beginPath();ctx.rect(0,0,s.w,s.h);ctx.clip();ctx.beginPath();setOwner(s.id);
      try{painters.get(s.id)({...api,ctx,t,local:t,w:s.w,h:s.h});}
      finally{setOwner('');ctx.restore();}
    }
  }
  function layout(textBoxes) {
    const issues=[],byId=new Map(states.map(s=>[s.id,s]));
    for(const s of states.filter(visible)){
      const r=s.rect,margin=W*.02;
      if(r.left<margin||r.right>W-margin||r.top<H*.03||r.bottom>H*.85)
        issues.push(`[对象 ${s.id}] 出画或进入字幕区域`);
      const a=D.presenter_area;
      if(a&&overlap(r,{left:a.x*W,top:a.y*H,right:(a.x+a.width)*W,bottom:(a.y+a.height)*H}))
        issues.push(`[对象 ${s.id}] 进入讲解员避让区域`);
      for(const other of states.filter(visible)){
        if(other.id<=s.id||!overlap(r,other.rect))continue;
        if(s.object.overlap_group&&s.object.overlap_group===other.object.overlap_group)continue;
        issues.push(`[对象 ${s.id}] 与 ${other.id} 重叠；有意复合须显式共享 overlap_group`);
      }
    }
    for(const b of textBoxes){
      const r={left:b.x,top:b.y,right:b.right,bottom:b.bottom};
      const owner=byId.get(b.owner);
      if(owner&&(r.left<owner.rect.left-1||r.top<owner.rect.top-1||r.right>owner.rect.right+1||r.bottom>owner.rect.bottom+1))
        issues.push(`[对象 ${b.owner}] 文字超出对象边界，不能依靠裁切隐藏`);
      for(const other of states.filter(visible)){
        if(other.id===b.owner||!overlap(r,other.rect))continue;
        if(owner?.object.overlap_group&&owner.object.overlap_group===other.object.overlap_group)continue;
        issues.push(`[对象 ${other.id}] 遮挡其他对象或背景文字：${b.text.slice(0,30)}`);
      }
    }
    for(const edge of edges)if(edge.status==='blocked')issues.push(`[关系 ${edge.id}] 无可用路径（${edge.source} -> ${edge.target}），已隐藏连线`);
    return issues;
  }
  return Object.freeze({register,check,render,layout,
    snapshot:()=>JSON.parse(JSON.stringify({revision:D.revision||'',objects:states.map(({object,...s})=>s),relations:edges}))});
}});
})();

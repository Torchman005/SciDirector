// Independent Canvas interpretation of X17; local fonts replace the named fonts.
// 来源：来自俯仰观照博主的动画画风整理 (wqb578/code-animation-341-styles).
// X-series source: code-video, Copyright (c) 2026 Bae-ChangHyun, MIT.
// https://github.com/wqb578/code-animation-341-styles/blob/01576924a2b316d1f11ae33f75d3c005e8781451/NOTICE.md
theme({paper:'#17508c',card:'#17508c',ink:'#f1f5f8',mute:'#b5cee4',line:'#6693bb',
  q:'#ffd166',k:'#ffd166',v:'#ffd166',gold:'#ffd166',blue:'#f1f5f8'});
scene('route', ({ctx,t,M,C,V,T,K,label,box}) => {
  const open=M.easeOutCubic(M.progress(t,0,T('route')+.7));
  const choose=M.easeInOut(M.progress(t,K('select','两个'),K('select','两个')+1));
  const merge=M.easeInOut(M.progress(t,T('merge'),T('merge')+2));
  const release=M.easeOutCubic(M.progress(t,T('merge')+2,T('merge')+2.8));
  const experts=V.experts.map((_,i)=>[1110,340+i*110]);
  const phase=t<T('select')?1:t<T('merge')?2:3;
  function stroke(points,color=C.ink,width=2,alpha=1) {
    ctx.save();ctx.globalAlpha*=alpha;ctx.strokeStyle=color;ctx.lineWidth=width;
    ctx.beginPath();points.forEach(([x,y],i)=>i?ctx.lineTo(x,y):ctx.moveTo(x,y));ctx.stroke();ctx.restore();
  }
  function dimension(a,b,y,text) {
    stroke([[a,y],[b,y]],C.mute,1);
    for(const x of [a,b]) stroke([[x-5,y+7],[x+5,y-7]],C.ink,2);
    ctx.fillStyle=C.paper;ctx.fillRect((a+b)/2-220,y-20,440,40);
    label(text,(a+b)/2,y,{size:26,color:C.mute});
  }
  function balloon(number,x,y,points) {
    stroke([[x,y+20],...points],C.mute,1.5);
    ctx.fillStyle=C.paper;ctx.strokeStyle=C.ink;ctx.lineWidth=2;
    ctx.beginPath();ctx.arc(x,y,23,0,Math.PI*2);ctx.fill();ctx.stroke();
    label(number,x,y,{size:26});
  }
  function path(a,b,color,alpha=1,reveal=1) {
    ctx.save();ctx.globalAlpha*=alpha;ctx.strokeStyle=color;ctx.lineWidth=3;
    const mid=(a[0]+b[0])/2,length=Math.hypot(b[0]-a[0],b[1]-a[1])*1.4;
    ctx.setLineDash([length,length]);ctx.lineDashOffset=length*(1-reveal);
    ctx.beginPath();ctx.moveTo(...a);ctx.bezierCurveTo(mid,a[1],mid,b[1],...b);ctx.stroke();
    ctx.setLineDash([]);
    if(reveal>.97) stroke([[b[0]-11,b[1]-6],b,[b[0]-11,b[1]+6]],color,2);
    ctx.restore();
  }
  function signal(a,b,p) {
    const u=1-p,mid=(a[0]+b[0])/2;
    const x=u*u*u*a[0]+3*u*u*p*mid+3*u*p*p*mid+p*p*p*b[0];
    const y=a[1]+(b[1]-a[1])*p*p*(3-2*p);
    ctx.fillStyle=C.gold;ctx.beginPath();ctx.arc(x,y,7,0,Math.PI*2);ctx.fill();
    ctx.strokeStyle=C.gold;ctx.lineWidth=1.5;ctx.beginPath();ctx.arc(x,y,14,0,Math.PI*2);ctx.stroke();
  }
  // The drawing sheet stays still; white construction strokes support yellow signals.
  ctx.globalAlpha=.13;
  for(let x=64;x<=1856;x+=32) stroke([[x,108],[x,880]],C.ink,1);
  for(let y=108;y<=880;y+=32) stroke([[64,y],[1856,y]],C.ink,1);
  ctx.globalAlpha=.8;stroke([[64,108],[1856,108],[1856,880],[64,880],[64,108]],C.ink,1);
  ctx.globalAlpha=open;
  label('计算，按需发生',100,164,{size:68,weight:800,align:'left'});
  label(`0${phase} / 03`,1810,165,{size:38,align:'right',color:C.gold});
  dimension(100,1810,236,'INPUT → ROUTER → EXPERTS → MERGE');
  label('NOTE  按需分配计算 · 所有权重均为示意',100,280,{size:28,align:'left',color:C.mute});
  ctx.setLineDash([8,8]);stroke([[100,306],[790,306]],C.mute,1);ctx.setLineDash([]);

  path([315,500],[550,500],C.ink,1,open);
  experts.forEach((p,i)=>{
    const selected=V.selected.includes(i);
    path([770,500],[1008,p[1]],selected&&choose>.02?C.gold:C.ink,
      selected?.35+.65*choose:.35*(1-.7*choose),open);
    if(selected&&merge>0) path([1212,p[1]],[1500,500],C.gold,merge,merge);
  });
  // Three stacked vector sheets, with shared coordinates for path and object ports.
  for(let i=0;i<V.signal.length;i++) {
    const x=173+i*17,y=427+i*15;
    box(x,y,106,106,{stroke:C.ink,radius:0,lineWidth:2});
    if(i===V.signal.length-1) for(let k=0;k<V.signal.length;k++) {
      stroke([[x+21+k*23,y+80],[x+21+k*23,y+80-V.signal[k]*65]],C.gold,7);
    }
  }
  label('输入向量',240,648,{size:34});
  balloon('01',240,365,[[240,414]]);

  ctx.save();ctx.translate(660,500);
  ctx.fillStyle=C.paper;ctx.strokeStyle=C.ink;ctx.lineWidth=3;
  ctx.beginPath();ctx.arc(0,0,108,0,2*Math.PI);ctx.fill();ctx.stroke();
  ctx.setLineDash([4,7]);ctx.beginPath();ctx.arc(0,0,91,0,2*Math.PI);ctx.stroke();ctx.setLineDash([]);
  for(let i=0;i<8;i++) {
    ctx.save();ctx.rotate(i*Math.PI/4+.5*choose);
    stroke([[48,-35],[78,-7],[55,25]],i<2&&choose>.1?C.gold:C.mute,3);ctx.restore();
  }
  label(choose>.5?String(V.selected.length):'R',0,2,{size:76,color:choose>.5?C.gold:C.ink});
  ctx.restore();
  label('路由器',660,648,{size:34});
  label(choose>.1?`本次选中 ${V.selected.length} / ${V.experts.length}`:'为当前输入选择路径',660,708,{size:28,color:C.mute});
  balloon('02',660,352,[[660,392]]);

  experts.forEach((p,i)=>{
    const active=V.selected.includes(i),lit=active&&choose>.15;
    ctx.save();ctx.globalAlpha*=active?1:1-.65*choose;
    box(p[0]-96,p[1]-38,192,76,{fill:lit?C.gold:C.paper,stroke:lit?C.gold:C.ink,radius:0,lineWidth:2});
    for(const x of [p[0]-103,p[0]+103]) for(const y of [-20,0,20]) stroke([[x-5,p[1]+y],[x+5,p[1]+y]],C.ink,2);
    label(V.experts[i],...p,{size:30,color:lit?C.paper:C.ink});
    ctx.restore();
  });
  label('专家组',1110,758,{size:30,color:C.mute});
  balloon('03',1305,340,[[1255,340],[1213,340]]);

  box(1507,425,186,150,{stroke:merge>.02?C.gold:C.ink,radius:0,lineWidth:3});
  label('Σ',1600,499,{size:92,color:merge>.02?C.gold:C.ink});
  label('加权汇合',1600,648,{size:34});
  if(merge>.05) label(V.weights.map(v=>`${Math.round(v*100)}%`).join(' + '),1600,708,{size:34,color:C.gold});
  balloon('04',1738,372,[[1738,425],[1694,425]]);

  // Drawing number, sheet name and fixed specimen date: no wall-clock dependency.
  box(1390,774,428,85,{stroke:C.mute,radius:0,lineWidth:1});
  stroke([[1390,814],[1818,814]],C.mute,1);
  label('MOE-017  /  X17',1410,795,{size:26,align:'left'});
  label('路由示意 · 2026.10.31',1410,836,{size:26,align:'left',color:C.mute});
  label(phase===1?'输入进入计算分配中心':phase===2?'只有亮起的专家参与本次计算':'选中结果按示意权重汇合',100,830,
    {size:30,color:C.ink,align:'left'});
  if(release>0) {
    ctx.save();ctx.globalAlpha*=release;ctx.translate(1240,819);ctx.rotate(-.1);
    box(-85,-30,170,60,{stroke:C.gold,radius:0,lineWidth:3});
    label('示意图',0,0,{size:34,color:C.gold});ctx.restore();
  }
  if(t>=T('route')&&t<T('select')) signal([315,500],[550,500],M.progress(t,T('route'),T('select')));
  if(choose>.01&&t<T('merge')) for(const i of V.selected)
    signal([770,500],[1008,experts[i][1]],M.progress(t,K('select','两个'),T('merge')));
  if(merge>0&&merge<1) for(const i of V.selected) signal([1212,experts[i][1]],[1500,500],merge);
});

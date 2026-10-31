// Hand-authored integration specimen. Scientific values are explicitly illustrative.
scene('route', ({ctx,t,M,C,V,W,H,T,K,label,box}) => {
  const enter = M.easeOutCubic(M.progress(t, 0, T('route') + .6));
  const choose = M.easeInOut(M.progress(t, K('select','两个'), K('select','两个') + .8));
  const merge = M.easeInOut(M.progress(t, T('merge'), T('merge') + 1.2));
  const end = M.easeInOut(M.progress(t, T('merge') + 1.2, T('merge') + 2.4));
  const points = V.experts.map((_,i) => [1150, 260 + i * 155]);
  const input = [230,500], gate = [690,500], output = [1660,500];
  ctx.globalAlpha = enter;
  label('让计算走向需要它的地方', 100, 160, {size:56,weight:800,align:'left'});
  label('Mixture of Experts · 混合专家', 102, 220, {size:28,color:C.mute,align:'left'});
  // A drafting surface: restrained and stable, while the signal carries the motion.
  ctx.strokeStyle = C.line; ctx.lineWidth = 1; ctx.globalAlpha = .35 * enter;
  for (let x=100; x<1850; x+=45) for (let y=300; y<820; y+=45) {
    ctx.beginPath(); ctx.arc(x,y,1,0,Math.PI*2); ctx.stroke();
  }
  ctx.globalAlpha = enter;
  function routePath(a,b,color,alpha=1) {
    ctx.save(); ctx.globalAlpha *= alpha; ctx.strokeStyle=color; ctx.lineWidth=4;
    ctx.beginPath(); ctx.moveTo(...a); ctx.bezierCurveTo((a[0]+b[0])/2,a[1],(a[0]+b[0])/2,b[1],...b); ctx.stroke();
    ctx.fillStyle=color; ctx.beginPath(); ctx.moveTo(...b); ctx.lineTo(b[0]-13,b[1]-7); ctx.lineTo(b[0]-13,b[1]+7); ctx.closePath(); ctx.fill();
    ctx.restore();
  }
  function routePoint(a,b,p) {
    const u=1-p, middle=(a[0]+b[0])/2;
    return [u*u*u*a[0]+3*u*u*p*middle+3*u*p*p*middle+p*p*p*b[0],
      a[1]+(b[1]-a[1])*p*p*(3-2*p)];
  }
  routePath([302,500],[565,500],C.blue);
  points.forEach((p,i)=>{
    const selected=V.selected.includes(i), color=selected?C.v:C.line;
    routePath([805,500],[p[0]-77,p[1]],color,selected?.3+.7*choose:.5*(1-.65*choose));
    if(selected) routePath([p[0]+77,p[1]],[1560,500],C.k,.15+.85*merge);
  });
  // Input is one persistent object, then an iris selects routes, then signals reunite.
  ctx.save(); ctx.translate(...input); ctx.rotate(-.09*(1-enter));
  V.signal.forEach((v,i)=>box(-65+i*17,-62+i*13,110,110,{fill:i===V.signal.length-1?C.blue:C.card,stroke:C.blue,radius:12}));
  ctx.restore();
  label('输入向量',input[0]+20,640,{size:34,color:C.blue});
  ctx.save(); ctx.translate(...gate);
  ctx.fillStyle=C.card; ctx.beginPath(); ctx.arc(0,0,110,0,2*Math.PI); ctx.fill();
  ctx.strokeStyle=C.ink; ctx.lineWidth=5; ctx.stroke();
  for(let i=0;i<8;i++) {
    const a=i*Math.PI/4 + .25*choose;
    ctx.save(); ctx.rotate(a); ctx.fillStyle=i%2?C.line:C.gold;
    ctx.beginPath(); ctx.moveTo(43,-8); ctx.lineTo(86,-18); ctx.lineTo(95,6); ctx.lineTo(51,19); ctx.closePath(); ctx.fill(); ctx.restore();
  }
  ctx.fillStyle=C.ink; ctx.beginPath(); ctx.arc(0,0,33,0,2*Math.PI); ctx.fill();
  ctx.restore();
  label('路由器',gate[0],660,{size:38});
  label(choose>.1?`此例选择 ${V.selected.length} 位专家`:'为当前输入选择路径',gate[0],718,{size:28,color:C.mute});
  points.forEach((p,i)=>{
    const selected=V.selected.includes(i), color=selected?C.v:C.mute;
    const activation=selected?choose:0;
    ctx.save(); ctx.globalAlpha*=selected?1:1-.6*choose;
    ctx.translate(...p);
    const s=1+.05*M.springAt(Math.max(0,t-T('select')),{stiffness:150,damping:18})*activation;
    ctx.scale(s,s);
    box(-70,-56,140,112,{fill:selected&&choose>.3?C.v:C.card,stroke:color,radius:20,lineWidth:3});
    ctx.strokeStyle=color; ctx.lineWidth=3;
    for(let k=0;k<4;k++) {
      const yy=-35+k*23;
      ctx.beginPath(); ctx.moveTo(-88,yy); ctx.lineTo(-72,yy); ctx.moveTo(72,yy); ctx.lineTo(88,yy); ctx.stroke();
    }
    ctx.restore();
    ctx.save(); ctx.globalAlpha*=selected?1:1-.6*choose;
    label(V.experts[i],p[0],p[1],{size:32,color:selected&&choose>.3?C.card:C.ink}); ctx.restore();
  });
  box(1568,422,184,156,{fill:C.card,stroke:merge>.1?C.k:C.line,radius:28,lineWidth:4});
  for(let i=0;i<V.selected.length;i++) {
    ctx.fillStyle=i?C.v:C.blue;
    ctx.globalAlpha=enter*(.12+.88*end);
    ctx.fillRect(1594+i*62,451,42,98*V.weights[i]);
  }
  ctx.globalAlpha=enter;
  label('加权汇合',1660,640,{size:34,color:C.k});
  if(end>.1) label('示意权重 '+V.weights.map(v=>`${Math.round(v*100)}%`).join(' / '),1660,705,{size:28,color:C.mute});
  // Signals move along shared paths; all positions derive from absolute t.
  if(t>=T('route')&&t<T('select')+.9) {
    const p=M.pointAlongPolyline([[305,500],[558,500]],M.progress(t,T('route'),T('select')+.9));
    ctx.fillStyle=C.gold; ctx.beginPath();ctx.arc(...p,12,0,Math.PI*2);ctx.fill();
  }
  if(choose>.05&&merge<1) for(const i of V.selected) {
    const a=[805,500], b=[points[i][0]-77,points[i][1]], p=M.easeInOut(M.progress(t,T('select')+.5,T('merge')));
    const [x,y]=routePoint(a,b,p);
    ctx.fillStyle=C.gold;ctx.beginPath();ctx.arc(x,y,10,0,Math.PI*2);ctx.fill();
  }
  if(merge>.01&&merge<1) for(const i of V.selected) {
    const a=[points[i][0]+77,points[i][1]], b=[1560,500];
    const [x,y]=routePoint(a,b,merge);
    ctx.fillStyle=C.k;ctx.beginPath();ctx.arc(x,y,10,0,Math.PI*2);ctx.fill();
  }
});

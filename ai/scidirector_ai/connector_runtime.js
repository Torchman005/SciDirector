/* Shared deterministic connector geometry for DOM scenes and fy Canvas. */
(()=>{
'use strict';
  const ROUTE_CLEARANCE=12, ROUTE_BEND_COST=40, ROUTE_DIAGONAL_COST=90, ROUTE_SNAP=24;
  const ports=(r,selected='auto')=>[
    {name:'right',x:r.right,y:(r.top+r.bottom)/2,ox:1,oy:0},
    {name:'left',x:r.left,y:(r.top+r.bottom)/2,ox:-1,oy:0},
    {name:'bottom',x:(r.left+r.right)/2,y:r.bottom,ox:0,oy:1},
    {name:'top',x:(r.left+r.right)/2,y:r.top,ox:0,oy:-1},
  ].filter(p=>selected==='auto'||p.name===selected);
  const crosses=(a,b,r)=>{
    const dx=b.x-a.x,dy=b.y-a.y;
    let lo=0,hi=1;
    for(const [p,q] of [[-dx,a.x-r.left],[dx,r.right-a.x],[-dy,a.y-r.top],[dy,r.bottom-a.y]]){
      if(Math.abs(p)<1e-9){if(q<=0)return false;continue;}
      const t=q/p;
      if(p<0)lo=Math.max(lo,t);else hi=Math.min(hi,t);
      if(lo>=hi)return false;
    }
    return (hi-lo)*Math.hypot(dx,dy)>1;
  };
  const routeConnector=(source,target,obstacles,{width:W,height:H,sourcePort='auto',targetPort='auto'})=>{
    let best=null,bestCost=Infinity;
    const lanesX=[...new Set(obstacles.flatMap(o=>[o.rect.left-ROUTE_CLEARANCE,o.rect.right+ROUTE_CLEARANCE]))].sort((a,b)=>a-b);
    const lanesY=[...new Set(obstacles.flatMap(o=>[o.rect.top-ROUTE_CLEARANCE,o.rect.bottom+ROUTE_CLEARANCE]))].sort((a,b)=>a-b);
    const axis=(a,b)=>Math.abs(a.x-b.x)<.5||Math.abs(a.y-b.y)<.5;
    const consider=(points,extra=0)=>{
      const path=points.filter((p,i)=>!i || Math.abs(p.x-points[i-1].x)>.01 || Math.abs(p.y-points[i-1].y)>.01);
      if(path.length<2 || path.some(p=>p.x<0||p.x>W||p.y<0||p.y>H))return;
      // A route is either one straight segment or fully orthogonal; never a
      // diagonal stitched to orthogonal stubs.
      if(path.length>2&&path.some((p,i)=>i&&!axis(path[i-1],p)))return;
      const first=path[0],second=path[1],last=path[path.length-1],before=path[path.length-2];
      if((second.x-first.x)*first.ox+(second.y-first.y)*first.oy<=0 ||
         (before.x-last.x)*last.ox+(before.y-last.y)*last.oy<=0)return;
      let cost=extra;
      for(let i=1;i<path.length;i++){
        const a=path[i-1],b=path[i];
        for(const obstacle of obstacles){
          if((i===1&&obstacle.id===source.id)||(i===path.length-1&&obstacle.id===target.id))continue;
          const r=obstacle.id===source.id || obstacle.id===target.id?obstacle.rect:
            {left:obstacle.rect.left-ROUTE_CLEARANCE,right:obstacle.rect.right+ROUTE_CLEARANCE,
              top:obstacle.rect.top-ROUTE_CLEARANCE,bottom:obstacle.rect.bottom+ROUTE_CLEARANCE};
          if(crosses(a,b,r))return;
        }
        const length=Math.hypot(b.x-a.x,b.y-a.y);
        cost+=length;
        if(!axis(a,b))cost+=ROUTE_DIAGONAL_COST;
        // Very short legs read as jogs; make them expensive.
        if(path.length>2&&length<28)cost+=ROUTE_BEND_COST;
        if(i>1){
          const prev=path[i-2];
          if(Math.abs((a.x-prev.x)*(b.y-a.y)-(a.y-prev.y)*(b.x-a.x))>.5)cost+=ROUTE_BEND_COST;
        }
      }
      if(cost<bestCost-1e-6){best=path;bestCost=cost;}
    };
    const s=source.rect,t=target.rect;
    const pairs=ports(s,sourcePort).flatMap(from=>ports(t,targetPort).map(to=>[from,to]));
    for(const [from,to] of pairs){
      const pt=(x,y,port)=>({x,y,ox:port.ox,oy:port.oy});
      const horizontal=from.ox!==0,toHorizontal=to.ox!==0;
      // Nearly aligned facing ports snap to one straight line in the shared span.
      if(sourcePort==='auto'&&targetPort==='auto'&&horizontal&&toHorizontal&&from.ox===-to.ox&&Math.abs(from.y-to.y)<ROUTE_SNAP){
        const lo=Math.max(s.top,t.top)+6,hi=Math.min(s.bottom,t.bottom)-6;
        if(lo<=hi){const y=Math.min(hi,Math.max(lo,(from.y+to.y)/2));consider([pt(from.x,y,from),pt(to.x,y,to)]);}
      }
      if(sourcePort==='auto'&&targetPort==='auto'&&!horizontal&&!toHorizontal&&from.oy===-to.oy&&Math.abs(from.x-to.x)<ROUTE_SNAP){
        const lo=Math.max(s.left,t.left)+6,hi=Math.min(s.right,t.right)-6;
        if(lo<=hi){const x=Math.min(hi,Math.max(lo,(from.x+to.x)/2));consider([pt(x,from.y,from),pt(x,to.y,to)]);}
      }
      consider([from,to]);
      if(horizontal&&toHorizontal){
        consider([from,{x:(from.x+to.x)/2,y:from.y},{x:(from.x+to.x)/2,y:to.y},to]);
        for(const x of lanesX)consider([from,{x,y:from.y},{x,y:to.y},to]);
      }else if(!horizontal&&!toHorizontal){
        consider([from,{x:from.x,y:(from.y+to.y)/2},{x:to.x,y:(from.y+to.y)/2},to]);
        for(const y of lanesY)consider([from,{x:from.x,y},{x:to.x,y},to]);
      }else if(horizontal){
        consider([from,{x:to.x,y:from.y},to]);
      }else{
        consider([from,{x:from.x,y:to.y},to]);
      }
    }
    if(best)return best;
    // Last resort: leave both ports, then cross over on a lane or midpoint.
    for(const [from,to] of pairs){
      const a={x:from.x+from.ox*ROUTE_CLEARANCE*2,y:from.y+from.oy*ROUTE_CLEARANCE*2};
      const b={x:to.x+to.ox*ROUTE_CLEARANCE*2,y:to.y+to.oy*ROUTE_CLEARANCE*2};
      const mx=(a.x+b.x)/2,my=(a.y+b.y)/2;
      consider([from,a,{x:mx,y:a.y},{x:mx,y:b.y},b,to],ROUTE_BEND_COST);
      consider([from,a,{x:a.x,y:my},{x:b.x,y:my},b,to],ROUTE_BEND_COST);
      for(const x of lanesX)consider([from,a,{x,y:a.y},{x,y:b.y},b,to],ROUTE_BEND_COST);
      for(const y of lanesY)consider([from,a,{x:a.x,y},{x:b.x,y},b,to],ROUTE_BEND_COST);
    }
    return best;
  };
  window.SCIDRouting=Object.freeze({route:routeConnector,crosses});
})();

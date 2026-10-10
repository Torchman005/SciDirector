/* Original cel-shaded science presenter. No remote assets or wall-clock state. */
function createScienceCharacter(root, svgNode, accent) {
  const svg=svgNode('svg',root,{viewBox:'0 0 240 360','aria-label':'二次元科学讲解员'});
  const ink='#25334B', skin='#FFE2CE', hair='#293850', paper='#F4F8FF';
  const path=(parent,d,fill,extra={})=>svgNode('path',parent,{d,fill,stroke:ink,'stroke-width':3,'stroke-linejoin':'round',...extra});
  svgNode('ellipse',svg,{cx:120,cy:342,rx:66,ry:9,fill:'#172A45',opacity:.18});
  const body=svgNode('g',svg,{});
  path(body,'M75 214 Q120 185 165 214 L178 313 Q120 336 62 313 Z',paper);
  path(body,'M99 214 L120 238 L142 214 L142 315 L99 315 Z',accent);
  path(body,'M88 211 L107 246 L93 261 L116 292 L107 319 L63 311 Z','#DAE5F3');
  path(body,'M151 211 L133 246 L149 261 L126 292 L134 319 L177 311 Z',paper);
  path(body,'M77 315 L111 319 L108 341 L71 341 Z',hair);
  path(body,'M129 319 L163 315 L170 341 L132 341 Z',hair);
  svgNode('rect',body,{x:147,y:271,width:19,height:22,rx:3,fill:accent});
  const left=svgNode('g',body,{}), right=svgNode('g',body,{});
  path(left,'M78 218 Q58 218 52 235 L38 281 Q40 296 54 292 L84 246 Z',paper);
  path(left,'M40 278 Q31 281 35 295 Q43 306 54 292 L54 280 Z',skin);
  path(right,'M162 218 Q182 217 188 234 L202 280 Q200 296 185 290 L155 245 Z',paper);
  path(right,'M186 279 L202 278 Q212 288 203 299 Q192 304 185 289 Z',skin);
  const head=svgNode('g',body,{});
  path(head,'M51 132 Q37 51 117 38 Q202 36 196 138 L184 211 L56 210 Z',hair);
  path(head,'M103 184 L137 184 L139 214 Q119 231 101 213 Z',skin);
  path(head,'M59 117 Q55 68 119 66 Q185 67 182 121 L174 165 Q154 198 120 199 Q86 198 66 165 Z',skin);
  path(head,'M56 129 Q44 84 71 61 Q104 26 152 43 Q202 55 190 128 L167 96 L161 115 L126 79 L136 112 L104 89 L89 117 L87 93 Z',hair);
  path(head,'M62 100 Q74 52 120 50 Q86 68 77 106 Z','#526B86',{'stroke-width':0});
  const eyes=svgNode('g',head,{});
  for(const x of [92,148]) {
    svgNode('ellipse',eyes,{cx:x,cy:137,rx:12,ry:18,fill:paper,stroke:ink,'stroke-width':2});
    svgNode('ellipse',eyes,{cx:x+1,cy:140,rx:8,ry:13,fill:accent});
    svgNode('ellipse',eyes,{cx:x+2,cy:141,rx:4,ry:10,fill:ink});
    svgNode('circle',eyes,{cx:x-2,cy:132,r:4,fill:'#FFFFFF'});
    path(head,`M${x-13} 118 Q${x} 112 ${x+11} 118`,'none',{'stroke-width':3});
    svgNode('ellipse',head,{cx:x,cy:160,rx:13,ry:5,fill:'#EE9F97',opacity:.38});
  }
  const mouth=path(head,'M109 175 Q120 182 131 175','none');
  path(head,'M163 68 L180 78 L174 92 L158 81 Z',accent,{'stroke-width':2});
  return (seconds,state) => {
    body.setAttribute('transform',`translate(0 ${Math.sin(seconds*2.1)*1.4})`);
    const turn=state.expression==='curious'?-5:state.expression==='focused'?2:Math.sin(seconds*.8)*1.3;
    head.setAttribute('transform',`rotate(${turn} 120 205)`);
    const phase=seconds%4.7;
    const blink=phase>4.42?Math.max(.07,Math.abs((phase-4.56)/.14)):1;
    eyes.setAttribute('transform',`translate(0 137) scale(1 ${blink}) translate(0 -137)`);
    const poses={idle:[0,0],explain:[30,-32],point:[0,-95],think:[-105,0],wave:[0,-145+Math.sin(seconds*8)*8]};
    const to=poses[state.gesture]||poses.idle, from=poses[state.gestureFrom]||to;
    const q=state.gestureMix??1, mix=q*q*(3-2*q);
    const [a,b]=to.map((value,index)=>from[index]+(value-from[index])*mix);
    left.setAttribute('transform',`rotate(${a} 77 225)`);
    right.setAttribute('transform',`rotate(${b} 164 225)`);
    mouth.setAttribute('d',state.expression==='surprised'?'M115 172 Q120 168 125 172 Q130 184 120 186 Q110 184 115 172 Z':
      state.expression==='smile'?'M107 173 Q120 191 133 173 Z':state.expression==='focused'?'M111 176 L129 176':'M109 175 Q120 182 131 175');
    mouth.setAttribute('fill',['surprised','smile'].includes(state.expression)?'#AD626C':'none');
  };
}

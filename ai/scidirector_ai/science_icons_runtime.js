/* Authored vector vocabulary, no external images or arbitrary SVG from models. */
function createScienceIcon(svg, make, glyph, width, height, tint, background) {
  let updateAction=null;
  const gradientId='scid-icon-material-'+svg.parentNode.id;
  const defs=make('defs',svg,{}),gradient=make('linearGradient',defs,{id:gradientId,x1:0,y1:0,x2:1,y2:1});
  make('stop',gradient,{offset:0,'stop-color':tint,'stop-opacity':.45});
  make('stop',gradient,{offset:1,'stop-color':tint,'stop-opacity':.08});
  const g=make('g',svg,{transform:`translate(${(width-Math.min(width,height))/2} ${(height-Math.min(width,height))/2}) scale(${Math.min(width,height)/120})`,
    fill:'none',stroke:tint,'stroke-width':3,'stroke-linecap':'round','stroke-linejoin':'round'});
  const path=d=>make('path',g,{d});
  const rect=(x,y,w,h,r=5)=>{
    make('rect',g,{x:x+2,y:y+4,width:w,height:h,rx:r,fill:background,stroke:'none'});
    return make('rect',g,{x,y,width:w,height:h,rx:r,fill:`url(#${gradientId})`});
  };
  const accent=(tag,attrs)=>make(tag,g,{...attrs,stroke:tint});
  const fine=d=>make('path',g,{d,stroke:tint,'stroke-width':1.7,opacity:.65,fill:'none'});
  const fillPath=d=>make('path',g,{d,fill:`url(#${gradientId})`});
  // A bounded ground shadow anchors the object without hiding scientific marks.
  make('ellipse',g,{cx:60,cy:108,rx:45,ry:5,fill:tint,opacity:.12,stroke:'none'});
  if(glyph==='chip') {
    for(let i=0;i<4;i++){let p=36+i*16;path(`M${p} 12v12 M${p} 96v12 M12 ${p}h12 M96 ${p}h12`);}
    rect(24,24,72,72,8);rect(37,37,46,46,4);
    path('M45 53h30 M45 63h18 M45 73h25');
  } else if(glyph==='memory') {
    for(let i=0;i<3;i++){const y=18+i*28;rect(18,y,84,22,4);path(`M28 ${y+11}h40`);make('circle',g,{cx:90,cy:y+11,r:3,fill:tint,stroke:'none'});}
    path('M38 106h44');
  } else if(glyph==='factory') {
    const body=path('M16 100V52L44 32v20l28-20v20h32v48Z');body.setAttribute('fill',`url(#${gradientId})`);
    path('M82 50V17h16v33 M28 72h12v12H28Z M52 72h12v12H52Z M80 100V73h13v27');
    path('M85 10h11');
  } else if(glyph==='router') {
    rect(43,43,34,34,8);
    path('M60 13v30 M77 60h27 M60 77v27 M43 60H16');
    for(const [x,y] of [[60,15],[104,60],[60,104],[16,60]])make('circle',g,{cx:x,cy:y,r:7,fill:background});
    path('M51 61l7 7 13-17');
  } else if(glyph==='gate') {
    rect(24,14,72,88,9);path('M40 20v75 M56 20v75 M72 20v75 M88 20v75');
    make('rect',g,{x:31,y:45,width:57,height:21,rx:4,fill:background});path('M41 56h34 m-7-6 7 6-7 6');
  } else if(glyph==='lens') {
    make('circle',g,{cx:51,cy:48,r:32,fill:background});path('M75 72l28 28 M35 43h32 M51 28v32');
  } else if(glyph==='phone') {
    rect(31,9,58,100,11);
    make('rect',g,{x:37,y:19,width:46,height:73,rx:4,fill:background,stroke:tint,'stroke-width':2});
    fine('M52 14h16 M54 101h12');
    make('circle',g,{cx:60,cy:100,r:3.4,fill:background,stroke:tint,'stroke-width':1.5});
    rect(43,29,34,14,3);
    fine('M47 51h26 M47 58h22 M47 65h25 M47 72h18');
    accent('circle',{cx:51,cy:82,r:3,fill:background,'stroke-width':1.5});
    fine('M59 82h14');
  } else if(glyph==='calendar') {
    rect(17,20,86,82,7);
    make('path',g,{d:'M18 39h84',stroke:tint,'stroke-width':5,opacity:.65});
    for(const x of [36,60,84]){
      make('rect',g,{x:x-2,y:13,width:4,height:15,rx:2,fill:background,stroke:tint,'stroke-width':2});
    }
    for(let row=0;row<3;row++)for(let col=0;col<4;col++){
      const x=30+col*20,y=52+row*17;
      make('rect',g,{x:x-5,y:y-5,width:10,height:10,rx:2,fill:row===1&&col===2?tint:background,
        opacity:row===1&&col===2?0.66:1,stroke:tint,'stroke-width':1.3});
    }
    fine('M28 98h64');
  } else if(glyph==='book') {
    fillPath('M12 24q22-9 46 2v74q-22-10-46-2Z');
    fillPath('M62 26q24-11 46-2v74q-24-8-46 2Z');
    make('path',g,{d:'M60 29v73 M14 25v73 M106 25v73',stroke:tint,'stroke-width':3});
    for(const y of [42,53,64,75]){
      fine(`M23 ${y}q13-3 28 1 M70 ${y+1}q13-4 27-1`);
    }
    fine('M35 88q9 0 17 4 M72 92q10-4 18-3');
  } else if(glyph==='film') {
    rect(12,25,96,70,5);
    make('path',g,{d:'M13 43h94 M13 77h94',stroke:tint,'stroke-width':2});
    for(const x of [22,43,64,85])for(const y of [34,86])
      make('rect',g,{x:x-4,y:y-3,width:8,height:6,rx:1,fill:background,stroke:tint,'stroke-width':1.1});
    for(const x of [18,47,76]){
      make('rect',g,{x,y:49,width:25,height:22,rx:2,fill:background,stroke:tint,'stroke-width':1.6});
      fine(`M${x+4} 66l6-6 4 3 7-8`);
    }
  } else if(glyph==='scissors') {
    const upper=make('g',g,{}),lower=make('g',g,{});
    for(const half of [upper,lower]){
      make('path',half,{d:'M60 60 L108 57 Q111 61 106 65 L60 68 Z',
        fill:`url(#${gradientId})`,stroke:tint,'stroke-width':2.5});
      make('path',half,{d:'M68 62 L101 61',fill:'none',stroke:background,
        'stroke-width':1.6,opacity:.8});
      make('path',half,{d:'M61 64 L44 64',fill:'none',stroke:tint,'stroke-width':7});
      make('circle',half,{cx:31,cy:64,r:14,fill:background,stroke:tint,'stroke-width':5});
      make('circle',half,{cx:31,cy:64,r:7,fill:'none',stroke:tint,'stroke-width':1.5});
    }
    make('circle',g,{cx:60,cy:64,r:5,fill:background,stroke:tint,'stroke-width':2.5});
    updateAction=progress=>{
      const angle=23+Math.max(0,1-Math.min(1,progress))*12;
      upper.setAttribute('transform',`rotate(${-angle} 60 64)`);
      lower.setAttribute('transform',`rotate(${angle} 60 64)`);
    };
  } else if(glyph==='microscope') {
    fillPath('M38 25l15-9 30 35-13 11Z');
    rect(30,15,26,13,3);
    fine('M62 41l10-8 M73 54l10-8');
    make('path',g,{d:'M83 51q19 18 6 40 M42 93h53 M47 85h30',stroke:tint,'stroke-width':6});
    rect(29,73,48,8,2);
    make('circle',g,{cx:84,cy:65,r:7,fill:background,stroke:tint,'stroke-width':2});
    make('path',g,{d:'M34 100h65l6 8H27Z',fill:`url(#${gradientId})`});
    fine('M42 69h13');
  } else if(glyph==='parcel') {
    fillPath('M16 38l43-19 45 20-45 21Z');
    fillPath('M16 38v48l43 21V60Z');
    fillPath('M59 60v47l45-21V39Z');
    make('path',g,{d:'M16 38l43 22 45-21 M59 60v47 M59 19v41 M37 28l44 21',
      stroke:tint,'stroke-width':2.6});
    make('path',g,{d:'M34 48v47 M75 53v45',stroke:background,'stroke-width':4,opacity:.7});
    fine('M22 75l20 10 M79 76l19-9');
  }
  return updateAction;
}

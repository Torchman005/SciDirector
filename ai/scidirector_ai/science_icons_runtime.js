/* Authored vector vocabulary, no external images or arbitrary SVG from models. */
function createScienceIcon(svg, make, glyph, width, height, tint, background) {
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
  }
}

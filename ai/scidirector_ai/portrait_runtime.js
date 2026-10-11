/* Every pose and mouth frame is a user-supplied PNG, loaded before capture. */
window.setupPresenter = async config => {
  const canvas=document.getElementById('avatar');
  canvas.width=config.width;canvas.height=config.height;
  const context=canvas.getContext('2d'),pictures={};
  for(const pose of Object.values(config.portrait.states)){
    for(const name of [pose.image,pose.mouth_open].filter(Boolean)){
      if(pictures[name])continue;
      const picture=new Image();picture.src='https://scid.local/'+name;
      await picture.decode();pictures[name]=picture;
    }
  }
  const reference=pictures[config.portrait.states.neutral.image];
  const fit=Math.min(config.width/reference.width,config.height/reference.height)*.94;
  const draw=(state,mouth,alpha)=>{
    if(alpha<=0)return;
    const pose=config.portrait.states[state],picture=pictures[mouth>.12&&pose.mouth_open?pose.mouth_open:pose.image];
    context.globalAlpha=alpha;
    context.drawImage(picture,-reference.width*fit/2,-reference.height*fit,reference.width*fit,reference.height*fit);
  };
  window.seekPresenter=seconds=>{
    const t=Math.max(0,Math.min(config.duration,Number.isFinite(seconds)?seconds:0));
    const position=t*config.hz,index=Math.floor(position),part=position-index;
    const mouth=(config.envelope[index]||0)*(1-part)+(config.envelope[index+1]||0)*part;
    const cues=config.poseTimeline;let current=0;
    for(let i=1;i<cues.length;i++){if(cues[i].time>t)break;current=i;}
    const blend=current?Math.min(1,Math.max(0,(t-cues[current].time)/config.poseFade)):1;
    context.setTransform(1,0,0,1,0,0);context.globalAlpha=1;context.clearRect(0,0,canvas.width,canvas.height);
    context.translate(config.width/2,config.height*.98);
    const breath=1+.004*Math.sin(t*1.8);context.scale(1,breath);
    if(current)draw(cues[current-1].state,mouth,1-blend);
    draw(cues[current].state,mouth,blend);
    context.globalAlpha=1;
  };
  window.seekPresenter(0);
};

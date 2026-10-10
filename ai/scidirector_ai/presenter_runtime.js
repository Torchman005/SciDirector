/* Pinned Cubism 4 adapter. Absolute parameter values make random access repeatable. */
window.setupPresenter = async config => {
  const app=new PIXI.Application({view:document.getElementById('avatar'),width:config.width,height:config.height,
    backgroundAlpha:0,autoStart:false,antialias:true,preserveDrawingBuffer:true});
  const model=await PIXI.live2d.Live2DModel.from('https://scid.local/model.model3.json',
    {autoUpdate:false,autoInteract:false,motionPreload:0});
  app.stage.addChild(model);
  const size=Math.min(config.width/model.width,config.height/model.height)*.94;
  model.scale.set(size); model.anchor.set(.5,1);model.position.set(config.width/2,config.height*.98);
  const core=model.internalModel.coreModel;
  const ids=new Set(core._model.parameters.ids);
  const groups=model.internalModel.settings.groups||[];
  const lips=config.mouth_parameter?[config.mouth_parameter]:
    (groups.find(g=>g.Name==='LipSync')?.Ids||['ParamMouthOpenY']);
  if(!lips.length || lips.some(id=>!ids.has(id))) throw new Error('模型缺少口型参数，请在导入设置中指定实际的 MouthOpen 参数');
  core.saveParameters();model.deltaTime=0;
  const set=(id,value)=>{if(ids.has(id)) core.setParameterValueById(id,value);};
  window.seekPresenter = seconds => {
    const t=Math.max(0,Number.isFinite(seconds)?seconds:0);
    core.loadParameters();
    const sample=t*config.hz, i=Math.floor(sample), fraction=sample-i;
    const mouth=(config.envelope[i]||0)*(1-fraction)+(config.envelope[i+1]||0)*fraction;
    for(const id of lips) set(id,mouth);
    set('ParamAngleX',Math.sin(t*.7)*3);set('ParamAngleY',Math.sin(t*.9)*1.5);
    set('ParamAngleZ',Math.sin(t*.55)*1.2);set('ParamBreath',.5+.5*Math.sin(t*1.8));
    const phase=t%4.7, blink=phase>4.42?Math.max(.03,Math.abs((phase-4.56)/.14)):1;
    set('ParamEyeLOpen',blink);set('ParamEyeROpen',blink);
    core.update();model.deltaTime=0;app.renderer.render(app.stage);
  };
  window.seekPresenter(0);
};

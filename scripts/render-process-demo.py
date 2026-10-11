"""Offline scientific illustration acceptance: FFN mechanism, full frame + local avatar clearance."""
import argparse
from pathlib import Path

from scidirector_ai.scene import SceneSpec, compile_scene
from scidirector_ai.schemas import StyleGuide


def demo_scene():
    elements=[]
    def add(id,kind,box,**kw):
        elements.append(dict(id=id,kind=kind,box=dict(zip(('x','y','width','height'),box)),**kw))
    def label(id,text,box,size=32,**kw):
        add(id,'text',box,text=text,font_size=size,layer=5,**kw)
    label('chapter','INSIDE THE MODEL  /  模型里的加工工序',(.06,.055,.84,.055),30,color='secondary')
    label('headline','前馈网络：每个词元，单独加工',(.06,.14,.89,.09),48)
    label('input_label','输入词元',(.06,.30,.19,.07),34,color='accent')
    add('tokens','grid',(.06,.40,.18,.25),columns=2,cells=[dict(label=s,color='accent') for s in ['x₁','x₂','x₃','x₄']],font_size=34,
        keyframes=[dict(time=0,reveal=0),dict(time=.16)])
    for i,(name,glyph,tint) in enumerate([('up','factory','secondary'),('activate','gate','warning'),('down','chip','success')]):
        x=.31+i*.165
        add(name,'icon',(x,.34,.115,.22),glyph=glyph,color=tint,
            keyframes=[dict(time=.12+i*.18,opacity=0,dy=.02,scale=.92),dict(time=.23+i*.18,easing='spring')])
        label(name+'_label',['升维','非线性变换','降维'][i],(x-.025,.60,.16,.075),32,color=tint,align='center',
            keyframes=[dict(time=.23+i*.18,opacity=0),dict(time=.30+i*.18)])
    for i,(x,w,tint) in enumerate([(.25,.055,'accent'),(.434,.034,'secondary'),(.595,.038,'warning')]):
        add('transfer'+str(i),'polyline',(x,.44,w,.055),points=[dict(x=.08,y=.5),dict(x=.92,y=.5)],color=tint,arrow=True,flow=True,stroke_width=4,
            keyframes=[dict(time=.14+i*.18,reveal=0),dict(time=.25+i*.18)])
    # A complete result remains above the avatar, so the right side is still used.
    add('output','grid',(.825,.34,.11,.16),columns=1,cells=[dict(label='y',color='success')],font_size=36,
        keyframes=[dict(time=.72,reveal=0),dict(time=.82)])
    add('output_arrow','polyline',(.761,.39,.061,.07),points=[dict(x=.03,y=.5),dict(x=.93,y=.5)],color='success',flow=True,
        keyframes=[dict(time=.63,reveal=0),dict(time=.75)])
    label('caption','同一套变换，分别作用于每个词元',(.06,.77,.70,.07),34,
        keyframes=[dict(time=.69,opacity=0),dict(time=.81)])
    label('note','普通 FFN 流程示意 · 不代表实际维度与物理工厂',(.06,.86,.70,.06),28,color='muted')
    return SceneSpec(elements=elements,background='grid',explanation='普通逐词元前馈网络机制示意，升维→非线性→降维；符号仅表示输入输出，无虚构矩阵数值。右下角局部避让讲解员。')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--render',action='store_true');parser.add_argument('--output',default='.data/process-demo')
    args=parser.parse_args();root=Path(args.output).resolve();root.mkdir(parents=True,exist_ok=True)
    style=StyleGuide(animation_style='anime',theme='dark',min_font_size=28,primary_color='#F5C67E',background_color='#172330',
                     presenter_safe_area=dict(x=.78,y=.55,width=.22,height=.45))
    code=compile_scene(demo_scene(),width=1600,height=900,duration=10,style=style)
    (root/'index.html').write_text(code,encoding='utf-8')
    if args.render:
        from scidirector_ai.renderer import HtmlRenderer, RenderRequest
        from scidirector_ai.config import Settings
        from scidirector_ai.sandbox.runner import SandboxRunner
        result=HtmlRenderer(Settings(llm_provider='mock',sandbox_timeout_sec=180),'motion').render(
            RenderRequest(shot_id='process-demo',code=code,output_dir=root,duration_sec=10,width=1600,height=900,fps=24),SandboxRunner())
        print(result.video_path)
    else: print(root/'index.html')


if __name__=='__main__': main()

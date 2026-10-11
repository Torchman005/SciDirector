"""Deterministic, model-free FlashAttention art/motion acceptance sample."""
import argparse
from pathlib import Path

from scidirector_ai.scene import SceneSpec, compile_scene
from scidirector_ai.schemas import StyleGuide


def demo_scene():
    elements=[]
    def add(id,kind,box,**kw):
        elements.append(dict(id=id,kind=kind,box=dict(zip(('x','y','width','height'),box)),**kw))
    def text(id,label,box,size=32,**kw):
        add(id,'text',box,text=label,font_size=size,layer=5,**kw)
    text('chapter','GPU MEMORY / 分块注意力',(.06,.05,.88,.06),32,color='secondary')
    text('title','让数据流动，而不是反复搬运整张矩阵',(.06,.13,.90,.10),46)
    add('memory','rect',(.06,.30,.34,.42),color='accent',material='glass',layer=-2,stroke_width=2)
    add('sram','rect',(.55,.30,.39,.42),color='secondary',material='glass',layer=-2,stroke_width=2)
    text('memory_label','HBM · 显存',(.085,.33,.28,.075),32,color='accent')
    text('sram_label','SRAM · 片上工作区',(.58,.33,.32,.075),32,color='secondary')
    for row in range(3):
        for col in range(4):
            add(f'cell_{row}_{col}','rect',(.10+col*.066,.45+row*.067,.05,.05),color='accent',corner_radius=5,stroke_width=1,
                keyframes=[dict(time=0,opacity=0),dict(time=.10+col*.018+row*.022)],overlap_group='tiles')
    add('transfer','polyline',(.42,.50,.105,.08),points=[dict(x=0,y=.5),dict(x=1,y=.5)],color='secondary',arrow=True,
        keyframes=[dict(time=0,reveal=0),dict(time=.25,reveal=0),dict(time=.36)])
    text('transfer_label','按块加载',(.415,.40,.13,.08),32,align='center',color='secondary')
    # Q/K/V blocks are a schematic working set, not invented numeric measurements.
    for i,(name,tint) in enumerate([('Q','primary'),('K','accent'),('V','secondary')]):
        box=(.59+i*.105,.45,.08,.115)
        frames=[dict(time=0,opacity=0,dx=-.05),dict(time=.29+i*.08,opacity=0,dx=-.05),dict(time=.39+i*.08,easing='spring')]
        add('tile_'+name,'rect',box,color=tint,material='gradient',corner_radius=10,keyframes=frames)
        text('name_'+name,name,box,36,align='center',keyframes=frames)
    text('compute','局部计算 · 在线归一化',(.585,.60,.32,.075),32,
         keyframes=[dict(time=0,opacity=0),dict(time=.62,opacity=0),dict(time=.70)])
    add('output','rect',(.06,.78,.88,.11),color='success',material='glass',layer=-2,
        keyframes=[dict(time=0,opacity=0),dict(time=.70,opacity=0),dict(time=.80)])
    text('output_label','累积输出 O  ·  无需将完整注意力矩阵写回显存',(.085,.79,.82,.09),34,
        keyframes=[dict(time=0,opacity=0),dict(time=.72,opacity=0),dict(time=.82)])
    text('note','数据流示意，非芯片结构或比例图',(.06,.925,.86,.05),32,color='muted')
    return SceneSpec(elements=elements,background='gradient',explanation='Q/K/V 分块进入片上工作区，局部计算与在线归一化后累积输出；不展示虚构性能数字。')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',default='.data/attention-demo')
    parser.add_argument('--render',action='store_true')
    args=parser.parse_args()
    root=Path(args.output).resolve();root.mkdir(parents=True,exist_ok=True)
    code=compile_scene(demo_scene(),width=1600,height=900,duration=10,
                       style=StyleGuide(animation_style='anime',primary_color='#FFB487',background_color='#101A2D'))
    (root/'index.html').write_text(code,encoding='utf-8')
    if args.render:
        from scidirector_ai.renderer import HtmlRenderer, RenderRequest
        from scidirector_ai.config import Settings
        from scidirector_ai.sandbox.runner import SandboxRunner
        result=HtmlRenderer(Settings(llm_provider='mock',sandbox_timeout_sec=180),'motion').render(
            RenderRequest(shot_id='attention-demo',code=code,output_dir=root,duration_sec=10,width=1600,height=900,fps=24),SandboxRunner())
        print(result.video_path)
    else: print(root/'index.html')

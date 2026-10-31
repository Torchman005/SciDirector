"""Render a hand-authored fy Canvas integration specimen without model/TTS calls."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from scidirector_ai.chapter.compile import render_chapter
from scidirector_ai.chapter.fy import compile_fy, engine_info, preflight_fy
from scidirector_ai.chapter.fy_styles import load_fy_style
from scidirector_ai.chapter.narration import estimate_timing
from scidirector_ai.chapter.writer import ChapterScript, cue_problems, static_problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='.data/fy-canvas-demo')
    parser.add_argument('--chrome', default='')
    parser.add_argument('--no-video', action='store_true')
    parser.add_argument('--fps', type=int, default=24)
    parser.add_argument('--fy-style', choices=['X17'], default='', help='手工样片可演示 X17；其他编号用于 make-chapter.py')
    parser.add_argument('--fy-style-root', default='')
    args = parser.parse_args()
    script = ChapterScript.model_validate({
        'title': '让计算走向需要它的地方', 'series': 'MoE 组件试验',
        'data': {'experts': ['专家 A','专家 B','专家 C','专家 D'], 'selected':[0,2], 'weights':[.65,.35], 'signal':[.3,.8,.5]},
        'scenes': [{'id':'routing','title':'路由与汇合','stage':'同一个输入经过路由器，选择专家后加权汇合', 'lines':[
            {'id':'route','sub':'混合专家模型会先为输入选择计算路径。','visual':'向量进入路由器。','cues':['输入']},
            {'id':'select','sub':'以选择两个专家为例，其余专家不参与这次计算。','visual':'两条路径激活，其余专家变淡。','cues':['两个']},
            {'id':'merge','sub':'选中专家的结果加权汇合，继续送往下一层。','visual':'信号汇合为输出，权重明确标注示意。','cues':['汇合']}
        ]}]})
    timing = estimate_timing(script.chapter_lines())
    out = Path(args.output).resolve()
    if out.exists():
        parser.error('样片输出目录必须为新目录')
    style = load_fy_style(args.fy_style, args.fy_style_root or None, output=out) if args.fy_style else None
    example = 'fy_blueprint.js' if style else 'fy_routing.js'
    code = (Path(__file__).resolve().parents[1] / 'ai/scidirector_ai/chapter/examples' / example).read_text(encoding='utf-8')
    issues = static_problems(code,'route') + cue_problems(code,'route',timing)
    if issues:
        raise RuntimeError('\n'.join(issues))
    out.mkdir(parents=True, exist_ok=bool(style))
    target = out / 'index.html'
    target.write_text(compile_fy([('route', code)],timing,title=script.title,series=script.series,data=script.data,fps=args.fps,style=style),encoding='utf-8')
    info = engine_info(style)
    info.update(timing_basis='estimated', specimen='hand_authored_not_model_generated', audio=None)
    if style:
        info.update(style_status='candidate_not_user_adopted',
                    adaptations=['独立 Canvas 演绎；未使用原版示例代码或音视频',
                                 'IBM Plex Mono / Nanum Gothic Coding 未安装；采用本机微软雅黑等回退字体'])
    (out/'engine.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'script.json').write_text(script.model_dump_json(indent=1),encoding='utf-8')
    (out/'timing.json').write_text(json.dumps(timing,ensure_ascii=False,indent=1),encoding='utf-8')
    issues = preflight_fy(target,timing,executable=args.chrome)
    (out/'preflight.json').write_text(json.dumps(issues,ensure_ascii=False,indent=1),encoding='utf-8')
    if issues:
        raise RuntimeError('\n'.join(issues))
    if not args.no_video:
        print(render_chapter(target,timing,out,fps=args.fps,executable=args.chrome))
    print(target)


if __name__ == '__main__':
    main()

"""Opt-in real-provider review/repair smoke test; writes artifacts, never fabricates a pass."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

from scidirector_ai.config import Settings
from scidirector_ai.media import extract_frames_with_times
from scidirector_ai.repair_evidence import precheck_repairs
from scidirector_ai.scene import compile_scene, extract_scene
from scidirector_ai.scene_revision import changed_elements
from scidirector_ai.schemas import SceneTag, ShotSpec, StyleGuide
from scidirector_ai.service import PipelineService


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live",action="store_true",help="Use configured real LLM/VLM providers (provider usage applies)")
    parser.add_argument("--output",default=".data/live-review-verification")
    parser.add_argument("--scene",choices=["reflection","attention"],default="reflection")
    args=parser.parse_args()
    if not args.live: parser.error("Pass --live to explicitly run real model calls")
    attention=args.scene=="attention"
    width,height,duration=(1600,900,10) if attention else (1280,720,8)
    spec=importlib.util.spec_from_file_location("demo",Path(__file__).with_name("render-attention-demo.py" if attention else "render-anime-demo.py"))
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    scene=module.demo_scene()
    for element in scene.elements:
        if element.id == "angles": element.text = "入射角 ≠ 反射角"  # deliberate, known scientific defect
        if element.id == "output_label": element.text = "必须把完整注意力矩阵反复写回显存" # deliberate known defect
    root=Path(args.output).resolve(); root.mkdir(parents=True,exist_ok=True)
    settings=Settings(tts_provider="none",postgres_dsn="",render_width=width,render_height=height,
                      render_fps=12,sandbox_timeout_sec=180,llm_timeout_sec=120,llm_max_retries=0)
    style=StyleGuide(animation_style="anime",min_font_size=32)
    service=PipelineService(settings)
    try:
        if service.llm.is_mock: raise RuntimeError("Configured LLM is mock; live validation requires real providers")
        shot=ShotSpec(shot_id=args.scene,index=1,tag=SceneTag.MOTION,duration_sec=duration,
                      narration=("FlashAttention 将 Q/K/V 按块加载到片上存储计算，在线归一化并累积输出 O，避免把完整注意力矩阵写回显存。" if attention else "光的反射遵循反射定律：入射角等于反射角，两个角都从法线量起。"),
                      visual_brief=("展示分块加载、片上计算与累积输出的科学关系。" if attention else "展示入射光线、法线、反射光线和正确的反射定律。"),
                      code=compile_scene(scene,width=width,height=height,duration=duration,style=style),language="html")
        before,_=service._render_shot(job_id="quality-live",shot=shot,code=shot.code,style_guide=style,
            attempt=1,output_dir=str(root/"before"))
        samples=extract_frames_with_times(before.video_path,root/"before/review",service.runner,
            count=4,duration_sec=duration,width=1024)
        before.frame_samples=[sample.path for sample in samples]
        outcome=service.critic.review(shot=shot,artifact=before,style_guide=style,attempt=1)
        feedback=outcome.feedback
        (root/"before.json").write_text(feedback.model_dump_json(indent=2),encoding="utf-8")
        print("before",feedback.passed,feedback.score,feedback.issues,flush=True)
        if outcome.degraded: raise RuntimeError("模型/证据不可用；停止修订，先解决审核依赖")
        if feedback.passed: raise AssertionError("VLM missed the deliberately incorrect scientific statement")
        revised,after,_=service.generate_shot(job_id="quality-live",shot=shot,attempt=2,style_guide=style,
            feedback=feedback,output_dir=str(root/"after"))
        (root/"after.html").write_text(revised.code,encoding="utf-8")
        current=extract_frames_with_times(after.video_path,root/"after/review",service.runner,
            count=4,duration_sec=duration,width=1024)
        after.frame_samples=[sample.path for sample in current]
        pairs=precheck_repairs(feedback,before,after,[{"ts":s.ts,"path":s.path} for s in samples],
                               current,root/"evidence",service.runner)
        final=service.critic.review(shot=revised,artifact=after,style_guide=style,attempt=2,
            previous_review=feedback,repair_prechecks=pairs).feedback
        result={"text_provider":service.llm.text.provider,"vision_provider":service.llm.vision.provider,
                "before":feedback.model_dump(mode="json"),"after":final.model_dump(mode="json"),
                "changed_ids":changed_elements(scene,extract_scene(revised.code)),
                "before_video":before.video_path,"after_video":after.video_path,"prechecks":pairs}
        (root/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps({"before_passed":feedback.passed,"after_passed":final.passed,
                          "changed_ids":result["changed_ids"],"result":str(root/"result.json")},ensure_ascii=False),flush=True)
        if not final.passed: raise AssertionError("Real model revision still requires review; see result.json")
    finally: service.close()


if __name__ == "__main__": main()

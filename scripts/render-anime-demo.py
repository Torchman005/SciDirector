"""Render a reproducible, model-free animation sample: python scripts/render-anime-demo.py."""
from pathlib import Path
import argparse

from scidirector_ai.scene import SceneSpec, compile_scene
from scidirector_ai.schemas import StyleGuide


def demo_scene() -> SceneSpec:
    return SceneSpec.model_validate({"elements": [
        {"id":"title","kind":"text","text":"光的反射：角度从法线量起","font_size":44,
         "box":{"x":.07,"y":.08,"width":.86,"height":.12}},
        {"id":"surface","kind":"rect","color":"#526B86",
         "box":{"x":.09,"y":.67,"width":.62,"height":.014}},
        {"id":"normal","kind":"polyline","color":"muted","points":[{"x":0,"y":0},{"x":0,"y":1}],
         "box":{"x":.4,"y":.28,"width":.001,"height":.39}},
        {"id":"incident","kind":"polyline","color":"#F7BE62","arrow":True,
         "points":[{"x":0,"y":0},{"x":1,"y":1}],
         "box":{"x":.203125,"y":.32,"width":.196875,"height":.35},
         "keyframes":[{"time":0,"reveal":0},{"time":.16,"reveal":0},{"time":.4}]},
        {"id":"reflected","kind":"polyline","color":"#67D4D2","arrow":True,
         "points":[{"x":0,"y":1},{"x":1,"y":0}],
         "box":{"x":.4,"y":.32,"width":.196875,"height":.35},
         "keyframes":[{"time":0,"reveal":0},{"time":.42,"reveal":0},{"time":.66}]},
        {"id":"angles","kind":"text","text":"入射角 = 反射角","font_size":38,"align":"center",
         "box":{"x":.17,"y":.73,"width":.48,"height":.12},
         "keyframes":[{"time":0,"opacity":0},{"time":.64,"opacity":0},{"time":.76}]},
        {"id":"normal_label","kind":"text","text":"法线","font_size":32,
         "box":{"x":.42,"y":.24,"width":.13,"height":.08}},
        {"id":"host","kind":"character","color":"#67D4D2",
         "box":{"x":.74,"y":.27,"width":.18,"height":.48},
         "keyframes":[{"time":0,"opacity":0,"dy":.025,"expression":"curious","gesture":"think"},
                      {"time":.12,"expression":"curious","gesture":"think","easing":"spring"},
                      {"time":.4,"expression":"focused","gesture":"point"},
                      {"time":.75,"expression":"smile","gesture":"explain"}]}
    ],"explanation":"法线为界面垂线；两光线与法线夹角相等。先入射后反射，角色不遮挡主图。"})


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",default=".data/anime-demo")
    parser.add_argument("--render",action="store_true")
    args=parser.parse_args()
    root=Path(args.output).resolve(); root.mkdir(parents=True,exist_ok=True)
    code=compile_scene(demo_scene(),width=1280,height=720,duration=8,
                       style=StyleGuide(animation_style="anime",primary_color="#67D4D2"))
    (root/"index.html").write_text(code,encoding="utf-8")
    if args.render:
        from scidirector_ai.renderer import HtmlRenderer, RenderRequest
        from scidirector_ai.config import Settings
        from scidirector_ai.sandbox.runner import SandboxRunner
        result=HtmlRenderer(Settings(llm_provider="mock",sandbox_timeout_sec=180),"motion").render(
            RenderRequest(shot_id="anime-demo",code=code,output_dir=root,duration_sec=8,width=1280,height=720,fps=24),
            SandboxRunner())
        print(result.video_path)
    else:
        print(root/"index.html")

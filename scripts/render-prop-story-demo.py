"""Model-free paper-stage acceptance scene for narrated prop choreography."""
import argparse
from pathlib import Path

from scidirector_ai.scene import SceneSpec, compile_scene
from scidirector_ai.schemas import StyleGuide


def demo_scene() -> SceneSpec:
    return SceneSpec.model_validate({"background": "paper_stage", "elements": [
        {"id": "title", "kind": "text", "text": "一段长视频，怎样变成几段清楚的解释？",
         "font_size": 54, "color": "text", "box": {"x": .07, "y": .075, "width": .86, "height": .12}},
        {"id": "film", "kind": "icon", "glyph": "film", "color": "secondary",
         "box": {"x": .06, "y": .22, "width": .24, "height": .40},
         "keyframes": [{"time": 0, "opacity": 0, "dy": -.04}, {"time": .15, "opacity": 1}]},
        {"id": "film_label", "kind": "text", "text": "原片",
         "font_size": 42, "color": "secondary", "align": "center", "layer": 5,
         "box": {"x": .07, "y": .625, "width": .22, "height": .09}},
        {"id": "cut", "kind": "icon", "glyph": "scissors", "color": "accent",
         "box": {"x": .38, "y": .25, "width": .21, "height": .38},
         "keyframes": [{"time": 0, "opacity": 0, "rotation": -8, "reveal": 0},
                       {"time": .32, "opacity": 1, "rotation": 8, "reveal": 0,
                        "easing": "spring"},
                       {"time": .55, "rotation": 0, "reveal": 1}]},
        {"id": "cut_label", "kind": "text", "text": "按主题剪辑",
         "font_size": 40, "color": "accent", "align": "center", "layer": 5,
         "box": {"x": .38, "y": .625, "width": .21, "height": .09},
         "keyframes": [{"time": .22, "opacity": 0}, {"time": .36, "opacity": 1}]},
        {"id": "clips", "kind": "grid", "color": "success", "font_size": 34,
         "columns": 1, "cells": [{"label": "问题"}, {"label": "证据"}, {"label": "结论"}],
         "box": {"x": .70, "y": .17, "width": .23, "height": .37},
         "keyframes": [{"time": 0, "opacity": 0, "reveal": 0},
                       {"time": .48, "opacity": 1, "reveal": 0},
                       {"time": .86, "reveal": 1}]},
        {"id": "cut_link", "kind": "line", "color": "muted", "arrow": True,
         "anchor_from": "film", "anchor_to": "cut",
         "keyframes": [{"time": .16, "reveal": 0}, {"time": .34, "reveal": 1}]},
        {"id": "result_link", "kind": "line", "color": "muted", "arrow": True,
         "anchor_from": "cut", "anchor_to": "clips",
         "keyframes": [{"time": .48, "reveal": 0}, {"time": .65, "reveal": 1}]},
        {"id": "explain", "kind": "text",
         "text": "先确认每一段的论点，再安排画面；剪辑只是让论点更容易看见。",
         "font_size": 39, "color": "text", "box": {"x": .08, "y": .76, "width": .65, "height": .13},
         "keyframes": [{"time": .6, "opacity": 0}, {"time": .76, "opacity": 1}]},
    ], "explanation": "原片、剪辑动作和三段论述按旁白先后出现；词语只是结构标签，不代表生成的真实数据。"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=".data/prop-story-demo.html")
    args = parser.parse_args()
    target = Path(args.output).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(compile_scene(demo_scene(), width=1600, height=900, duration=10,
                                    style=StyleGuide(preset="paper", background_style="paper_stage",
                                                     animation_style="anime",
                                                     presenter_safe_area={"x": .78, "y": .55,
                                                                          "width": .22, "height": .45})),
                      encoding="utf-8")
    print(target)

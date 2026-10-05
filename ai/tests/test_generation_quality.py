"""First-draft gates reject actual defects without adding full renders or VLM calls."""
from typing import Any
from pathlib import Path
from types import SimpleNamespace

import pytest

from scidirector_ai.agents.coder import CoderAgent
from scidirector_ai.config import Settings
from scidirector_ai.generation_quality import QualityResult, check_manim_structure, production_brief
from scidirector_ai.generation_quality import BrowserPreflight
from scidirector_ai.rag import FewShot, JsonCorpusRetriever
from scidirector_ai.schemas import RenderEngine, SceneTag, ShotSpec, StyleGuide
from test_coder import VALID_HTML, VALID_MANIM, _StubLLM, make_agent, make_shot


class Checker:
    def __init__(self, results: list[QualityResult]) -> None:
        self.results = list(results)
        self.calls: list[str] = []

    def check(self, code: str, shot: ShotSpec, style: StyleGuide) -> QualityResult:
        self.calls.append(code)
        return self.results.pop(0)


def test_layout_error_is_repaired_once_before_render() -> None:
    agent = make_agent([VALID_HTML, VALID_HTML.replace("chart", "fixed")])
    checker = Checker([QualityResult(checked=True, issues=["4.00s #label 文字被裁切；拆行"]), QualityResult(checked=True)])
    agent.quality_checker = checker
    result = agent.generate(shot=make_shot(SceneTag.DATA), style_guide=StyleGuide())
    assert result.policy_ok and result.quality.checked and result.llm_attempts == 2
    assert len(checker.calls) == 2
    assert "#label" in agent.llm.calls[1]["user"]  # type: ignore[attr-defined]
    assert VALID_HTML in agent.llm.calls[1]["user"]  # type: ignore[attr-defined]


def test_failed_repair_is_blocked_and_unavailable_is_explicit() -> None:
    agent = make_agent([VALID_HTML, VALID_HTML])
    checker = Checker([QualityResult(checked=True, issues=["裁切"]), QualityResult(checked=True, issues=["裁切"] )])
    agent.quality_checker = checker
    result = agent.generate(shot=make_shot(SceneTag.DATA), style_guide=StyleGuide())
    assert not result.policy_ok and result.llm_attempts == 2 and len(checker.calls) == 2
    agent = make_agent([VALID_HTML])
    agent.quality_checker = Checker([QualityResult(reason="浏览器不可用")])
    result = agent.generate(shot=make_shot(SceneTag.DATA), style_guide=StyleGuide())
    assert result.policy_ok and not result.quality.checked and result.quality.reason == "浏览器不可用"


def test_static_failure_never_executes_generated_code() -> None:
    agent = make_agent(["<div>missing seek</div>", "<div>still missing</div>"])
    checker = Checker([])
    agent.quality_checker = checker
    assert not agent.generate(shot=make_shot(SceneTag.DATA), style_guide=StyleGuide()).policy_ok
    assert checker.calls == []


@pytest.mark.parametrize("code", ["from manim import *\nclass Wrong(Scene): pass",
                                 "from manim import *\nclass SciShotScene(Scene):\n def construct(self): self.wait(8)"])
def test_manim_missing_scene_and_empty_output(code: str) -> None:
    assert not check_manim_structure(code).ok
    assert check_manim_structure(VALID_MANIM).ok


def test_generation_plan_uses_real_size_and_beats_without_extra_llm_call() -> None:
    settings = Settings(env="test", llm_provider="mock", render_width=1280, render_height=720)
    shot = make_shot(beats=["前提", "推导", "结论"], duration_sec=12)
    brief = production_brief(shot, StyleGuide(), settings)
    assert "1280×720" in brief and "0.00～4.00s：前提" in brief and "8.00～12.00s：结论" in brief
    agent = CoderAgent(_StubLLM([VALID_MANIM]), settings, JsonCorpusRetriever(()))  # type: ignore[arg-type]
    result = agent.generate(shot=shot, style_guide=StyleGuide())
    assert result.llm_attempts == 1 and "首稿实施约束" in agent.llm.calls[0]["user"]  # type: ignore[attr-defined]


def test_examples_never_cross_engines_and_zero_disables_rag() -> None:
    corpus = (FewShot(id="wrong", title="same topic", tag="DATA", engine="manim", summary="s", code="python"),
              FewShot(id="right", title="other topic", tag="AMBIENCE", engine="motion", summary="s", code=VALID_HTML))
    retriever = JsonCorpusRetriever(corpus)
    shot = ShotSpec(tag=SceneTag.DATA, engine=RenderEngine.MOTION)
    assert [e.id for e in retriever.retrieve(shot)] == ["right"]
    agent = make_agent([VALID_MANIM], rag_few_shot_k=0)
    result = agent.generate(shot=make_shot(), style_guide=StyleGuide())
    assert result.examples_used == []


def test_environment_timeout_is_bounded_and_cached(tmp_path: Path) -> None:
    settings = Settings(env="test", llm_provider="openai", sandbox_work_dir=str(tmp_path))
    checker = BrowserPreflight(settings)
    calls: list[dict[str, Any]] = []
    def run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(ok=False, summary=lambda: "timeout", tail=lambda n: "driver stuck")
    checker.runner = SimpleNamespace(run=run)  # type: ignore[assignment]
    shot = make_shot(SceneTag.DATA)
    result = checker.check(VALID_HTML, shot, StyleGuide())
    assert not result.checked and "timeout" in result.reason
    assert calls[0]["limits"].timeout_sec == 30
    assert "冷却" in checker.check(VALID_HTML, shot, StyleGuide()).reason
    assert len(calls) == 1


def test_corpus_geometry_uses_perpendicular_tangent_and_no_undefined_seed() -> None:
    from scidirector_ai.rag import load_corpus
    example = next(e for e in load_corpus() if e.id == "math-002")
    assert "random.seed" not in example.code
    assert "rotate_vector(radius.get_unit_vector(), PI / 2)" in example.code

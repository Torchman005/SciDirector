"""RAG 检索的测试。

检索质量直接决定"一次通过率"（本项目的北极星指标），
因此这里既验证**打分权重**这种可解释的行为，也验证**降级**路径
（语料缺失/损坏时不能把进程拖垮）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scidirector_ai.rag import (
    FewShot,
    JsonCorpusRetriever,
    build_retriever,
    load_corpus,
    store as rag_store,
)
from scidirector_ai.schemas import SceneTag, ShotSpec

# ---------------------------------------------------------------------------
# 语料本身
# ---------------------------------------------------------------------------


class TestShippedCorpus:
    """校验随包发布的语料真的可用 —— 语料坏了会静默拉低通过率。"""

    def test_corpus_loads(self) -> None:
        corpus = load_corpus()
        assert len(corpus) >= 6, f"随包语料只有 {len(corpus)} 条，覆盖不足"

    def test_every_entry_is_complete(self) -> None:
        for item in load_corpus():
            assert item.id, "范例缺少 id"
            assert item.title and item.summary, f"{item.id} 缺少标题或摘要"
            assert item.tag in {t.value for t in SceneTag}, f"{item.id} 标签非法：{item.tag}"
            assert item.keywords, f"{item.id} 没有关键词，检索会失效"
            assert len(item.summary) >= 30, f"{item.id} 的 summary 太短，无法说明范式要点"

    def test_html_examples_implement_the_render_contract(self) -> None:
        """HTML 范例必须实现 ``window.__seek`` 契约。

        范例是被模型**模仿**的；范例本身漏了契约，模型就会照抄一个
        渲染时静止的错误实现。
        """
        html_engines = {"d3", "echarts", "code_anim"}
        for item in load_corpus():
            if item.engine in html_engines:
                assert "window.__seek" in item.code, f"{item.id} 缺少 window.__seek 契约"
                assert "window.__ready" in item.code, f"{item.id} 缺少 window.__ready"

    def test_html_examples_do_not_use_cdn(self) -> None:
        """范例不能引用 CDN —— 沙盒没有网络，模型会照抄这个错误。"""
        for item in load_corpus():
            lowered = item.code.lower()
            for marker in ("cdn.jsdelivr.net", "unpkg.com", "cdnjs.", "src=\"http"):
                assert marker not in lowered, f"{item.id} 引用了外部资源：{marker}"

    def test_manim_examples_have_fixed_class_name(self) -> None:
        for item in load_corpus():
            if item.engine == "manim":
                assert "class SciShotScene(Scene)" in item.code, (
                    f"{item.id} 的类名必须固定为 SciShotScene"
                )

    def test_every_tag_has_at_least_one_example(self) -> None:
        tags = {item.tag for item in load_corpus()}
        assert tags == {t.value for t in SceneTag}, f"标签覆盖不全：{tags}"


# ---------------------------------------------------------------------------
# 打分（可解释）
# ---------------------------------------------------------------------------


def make_shot(**overrides: object) -> ShotSpec:
    base: dict[str, object] = {
        "shot_id": "job-x-s000",
        "index": 0,
        "narration": "展示公式推导过程",
        "visual_brief": "居中展示公式",
        "tag": SceneTag.MATH,
        "keywords": ["公式", "推导"],
    }
    base.update(overrides)
    return ShotSpec(**base)  # type: ignore[arg-type]


class TestScoring:
    def test_tag_match_dominates(self) -> None:
        """标签匹配权重最高：标签相同意味着渲染引擎相同，范式可直接迁移。"""
        example = FewShot(id="a", title="t", tag="MATH", engine="manim",
                          summary="s", code="c", keywords=[])
        score = JsonCorpusRetriever.score(
            example, tag="MATH", engine="", keywords=set(), haystack=""
        )
        assert score == 4.0

    def test_engine_match_adds_on_top(self) -> None:
        example = FewShot(id="a", title="t", tag="MATH", engine="manim",
                          summary="s", code="c", keywords=[])
        score = JsonCorpusRetriever.score(
            example, tag="MATH", engine="manim", keywords=set(), haystack=""
        )
        assert score == 6.0

    def test_keyword_overlap_is_capped(self) -> None:
        example = FewShot(id="a", title="t", tag="X", engine="y", summary="s", code="c",
                          keywords=["k1", "k2", "k3", "k4", "k5"])
        score = JsonCorpusRetriever.score(
            example, tag="", engine="", keywords={"k1", "k2", "k3", "k4", "k5"}, haystack=""
        )
        assert score == 3.0, "关键词得分必须封顶，否则关键词多的范例会垄断召回"

    def test_keyword_match_is_case_insensitive(self) -> None:
        example = FewShot(id="a", title="t", tag="X", engine="y", summary="s", code="c",
                          keywords=["MathTex"])
        score = JsonCorpusRetriever.score(
            example, tag="", engine="", keywords={"mathtex"}, haystack=""
        )
        assert score == 1.0

    def test_haystack_gives_fallback_signal(self) -> None:
        """兜底信号：标题/摘要里的词出现在分镜文本里。"""
        example = FewShot(id="a", title="勾股定理的应用", tag="X", engine="y",
                          summary="面积法证明", code="c", keywords=[])
        score = JsonCorpusRetriever.score(
            example, tag="", engine="", keywords=set(), haystack="讲讲勾股定理"
        )
        assert score > 0

    def test_unrelated_example_scores_zero(self) -> None:
        example = FewShot(id="a", title="完全不相关", tag="MATH", engine="manim",
                          summary="无关内容", code="c", keywords=["x"])
        score = JsonCorpusRetriever.score(
            example, tag="AMBIENCE", engine="stock", keywords={"y"}, haystack="天气不错"
        )
        assert score == 0.0


class TestRetrieval:
    def test_returns_most_relevant_first(self) -> None:
        retriever = JsonCorpusRetriever()
        results = retriever.retrieve(make_shot(), k=3)
        assert results, "数学分镜没有召回到任何范例"
        assert results[0].tag == "MATH", f"首条应当是 MATH 范例，实际 {results[0].tag}"

    def test_respects_k(self) -> None:
        retriever = JsonCorpusRetriever()
        assert len(retriever.retrieve(make_shot(), k=1)) == 1
        assert len(retriever.retrieve(make_shot(), k=0)) == 0

    def test_each_tag_retrieves_its_own_examples(self) -> None:
        """四种标签都要能召回到**同标签**范例 —— 否则 RAG 等于没生效。"""
        retriever = JsonCorpusRetriever()
        for tag in SceneTag:
            results = retriever.retrieve(make_shot(tag=tag, keywords=[]), k=1)
            assert results, f"{tag} 没有召回到任何范例"
            assert results[0].tag == tag.value, (
                f"{tag} 召回的第一条是 {results[0].tag}，标签不匹配"
            )

    def test_results_are_deterministic(self) -> None:
        """同分时按 id 稳定排序 —— 结果可复现对调试与缓存都很重要。"""
        retriever = JsonCorpusRetriever()
        first = [e.id for e in retriever.retrieve(make_shot(), k=3)]
        second = [e.id for e in retriever.retrieve(make_shot(), k=3)]
        assert first == second

    def test_min_score_filters_irrelevant(self) -> None:
        """低于阈值宁可不召回：塞入不相关范例比不召回更糟。"""
        corpus = (
            FewShot(id="x", title="无关", tag="MATH", engine="manim",
                    summary="无关", code="c", keywords=[]),
        )
        retriever = JsonCorpusRetriever(corpus, min_score=4.0)

        # 标签匹配 -> 4.0，刚好达标
        assert retriever.retrieve(make_shot(tag=SceneTag.MATH), k=3)
        # 标签不匹配 -> 0 分，被过滤掉（宁可不召回，也不塞不相关的范例）
        assert retriever.retrieve(make_shot(tag=SceneTag.DATA, keywords=[]), k=3) == []

    def test_empty_corpus_is_safe(self) -> None:
        assert JsonCorpusRetriever(()).retrieve(make_shot(), k=3) == []
        assert JsonCorpusRetriever(()).size() == 0


# ---------------------------------------------------------------------------
# 降级：语料出问题时不能把进程拖垮
# ---------------------------------------------------------------------------


class TestDegradation:
    def test_missing_corpus_returns_empty(self, tmp_path: Path) -> None:
        """语料缺失只降低一次通过率，不该让进程起不来。"""
        load_corpus.cache_clear()
        assert load_corpus(str(tmp_path / "nope.json")) == ()
        load_corpus.cache_clear()

    def test_corrupt_corpus_returns_empty(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{ 这不是合法 JSON", encoding="utf-8")
        load_corpus.cache_clear()
        assert load_corpus(str(bad)) == ()
        load_corpus.cache_clear()

    def test_wrong_shape_returns_empty(self, tmp_path: Path) -> None:
        wrong = tmp_path / "wrong.json"
        wrong.write_text(json.dumps({"examples": "不是数组"}), encoding="utf-8")
        load_corpus.cache_clear()
        assert load_corpus(str(wrong)) == ()
        load_corpus.cache_clear()

    def test_non_dict_entries_are_skipped(self, tmp_path: Path) -> None:
        mixed = tmp_path / "mixed.json"
        mixed.write_text(
            json.dumps({"examples": [{"id": "ok", "tag": "MATH", "engine": "manim"}, "垃圾", 42]}),
            encoding="utf-8",
        )
        load_corpus.cache_clear()
        corpus = load_corpus(str(mixed))
        assert len(corpus) == 1 and corpus[0].id == "ok"
        load_corpus.cache_clear()

    def test_loader_is_cached(self) -> None:
        """重复装载必须命中缓存 —— 每个镜头都读盘会拖慢整条流水线。"""
        load_corpus.cache_clear()
        first = load_corpus()
        second = load_corpus()
        assert first is second, "语料没有被缓存"
        load_corpus.cache_clear()


def test_build_retriever_returns_working_instance() -> None:
    retriever = build_retriever()
    assert retriever.size() > 0
    assert retriever.retrieve(make_shot(), k=2)


def test_default_corpus_path_exists() -> None:
    assert Path(rag_store._CORPUS_PATH).is_file(), "随包语料文件缺失"

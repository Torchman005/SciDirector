"""Few-shot 语料与检索实现。

检索打分是**可解释的**，刻意不用黑盒相似度：

    标签匹配          权重最高（4.0）  —— 标签相同意味着渲染引擎相同，范式可直接迁移
    引擎匹配          权重 2.0        —— 标签不同但引擎相同的例子仍有参考价值
    关键词重叠        每个 1.0，上限 3.0
    标题/摘要字符重叠  0.5 封顶       —— 兜底信号，避免完全无召回

为什么可解释很重要：当"一次通过率"下降时，我们需要能回答
"是召回的范例不对，还是提示词不行？"。黑盒相似度会让这个问题无法定位。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from ..config import Settings
from ..logging import get_logger
from ..schemas import ShotSpec

logger = get_logger(__name__)

#: 语料文件位置（包内）。
_CORPUS_PATH = Path(__file__).parent / "corpus" / "few_shots.json"


@dataclass
class FewShot:
    """一条 Few-shot 范例。"""

    id: str
    title: str
    tag: str
    engine: str
    summary: str
    code: str
    keywords: list[str] = field(default_factory=list)
    source: str = ""

    @classmethod
    def from_dict(cls, raw: dict) -> FewShot:
        return cls(
            id=str(raw.get("id", "")),
            title=str(raw.get("title", "")),
            tag=str(raw.get("tag", "AMBIENCE")).upper(),
            engine=str(raw.get("engine", "stock")),
            summary=str(raw.get("summary", "")),
            code=str(raw.get("code", "")),
            keywords=[str(k) for k in raw.get("keywords", [])],
            source=str(raw.get("source", "")),
        )


class FewShotRetriever(Protocol):
    """检索器协议。替换实现（例如换成 pgvector）时上层无需改动。"""

    def retrieve(self, shot: ShotSpec, k: int = 3) -> list[FewShot]:
        ...

    def size(self) -> int:
        ...


@lru_cache(maxsize=4)
def load_corpus(path: str | None = None) -> tuple[FewShot, ...]:
    """装载语料（进程内缓存）。

    语料损坏时**降级为空语料而不是抛错**：没有 Few-shot 只会降低一次通过率，
    而进程起不来会让整个系统不可用 —— 两者严重程度差得远。
    """
    target = Path(path) if path else _CORPUS_PATH
    if not target.is_file():
        logger.warning("Few-shot 语料不存在，检索将返回空结果", extra={"path": str(target)})
        return ()

    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "Few-shot 语料加载失败，检索将返回空结果",
            extra={"path": str(target), "error": str(exc)[:300]},
        )
        return ()

    items = payload.get("examples", payload) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        logger.warning("Few-shot 语料格式非法（期望数组或 {examples: [...]}）")
        return ()

    corpus = tuple(FewShot.from_dict(item) for item in items if isinstance(item, dict))
    logger.info("Few-shot 语料已加载", extra={"count": len(corpus), "path": str(target)})
    return corpus


class JsonCorpusRetriever:
    """基于 JSON 语料 + 可解释打分的检索器。"""

    def __init__(
        self, corpus: tuple[FewShot, ...] | None = None, *, min_score: float = 1.0
    ) -> None:
        self._corpus = corpus if corpus is not None else load_corpus()
        # 低于阈值宁可不召回：塞入一个不相关的范例会误导模型，
        # 比"没有范例"更糟。
        self._min_score = min_score

    def size(self) -> int:
        return len(self._corpus)

    def retrieve(self, shot: ShotSpec, k: int = 3) -> list[FewShot]:
        """返回与分镜最相关的 k 条范例（按相关性降序）。"""
        if not self._corpus or k <= 0:
            return []

        tag = shot.tag.value if shot.tag else ""
        engine = shot.engine.value if shot.engine else ""
        keywords = {kw.lower() for kw in (shot.keywords or [])}
        haystack = f"{shot.narration} {shot.visual_brief}".lower()

        scored: list[tuple[float, FewShot]] = []
        for example in self._corpus:
            score = self.score(example, tag=tag, engine=engine, keywords=keywords, haystack=haystack)
            if score >= self._min_score:
                scored.append((score, example))

        # 稳定排序：同分时按 id 排，保证结果可复现（对调试与缓存都很重要）。
        scored.sort(key=lambda pair: (-pair[0], pair[1].id))
        return [example for _, example in scored[:k]]

    @staticmethod
    def score(
        example: FewShot,
        *,
        tag: str,
        engine: str,
        keywords: set[str],
        haystack: str,
    ) -> float:
        """给单条范例打分。纯函数，便于单测直接验证权重行为。"""
        score = 0.0
        if tag and example.tag == tag:
            score += 4.0
        if engine and example.engine == engine:
            score += 2.0

        example_kw = {kw.lower() for kw in example.keywords}
        overlap = keywords & example_kw
        score += min(len(overlap), 3) * 1.0

        # 兜底：标题/摘要里的片段出现在分镜文本中，说明主题相关。
        for token in (example.title, example.summary):
            if any(term in haystack for term in _match_terms(token)):
                score += 0.5
        return score


def _tokens(text: str) -> list[str]:
    """按空白与常见标点切分，保留长度 ≥ 2 的片段。

    刻意不引入 jieba 之类的分词库：这里的用途只是"兜底加一点分"，
    为此增加一个依赖与一份词典并不划算。
    """
    for sep in " \t\n，,。.、；;：:（）()[]【】《》\"'":
        text = text.replace(sep, " ")
    return [t for t in text.split() if len(t) >= 2]


#: 连续的 CJK 片段。
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")


def _match_terms(text: str) -> set[str]:
    """产出用于"是否出现在分镜文本中"判断的片段集合。

    **为什么需要字符二元组**：中文没有空格，按标点切出来的往往是一整个短语
    （"勾股定理的应用"），它几乎不可能原样出现在别处 —— 实测这条兜底信号
    对中文完全不生效。二元组（"勾股"、"股定"、"定理"…）是中文里
    最廉价且相当有效的模糊匹配单位。

    权重只有 0.5，因此二元组带来的少量误命中只会在同分时影响排序，
    不会把不相关的范例抬进召回结果。
    """
    terms = set(_tokens(text.lower()))
    for run in _CJK_RUN.findall(text):
        for i in range(len(run) - 1):
            terms.add(run[i : i + 2])
    return terms


def build_retriever(settings: Settings | None = None) -> FewShotRetriever:
    """构造检索器。

    当前只有 JSON 语料一种实现；保留工厂函数是为了让阶段五切换到
    pgvector 时，调用点（``CoderAgent``）一行都不用改。
    """
    return JsonCorpusRetriever()

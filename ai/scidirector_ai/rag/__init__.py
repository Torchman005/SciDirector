"""RAG：Few-shot 优秀案例检索。

为什么需要它（这是整套系统里**性价比最高**的一块）：
大模型写 Manim/D3 代码时，一次成功率主要取决于"它见过多少同类好代码"。
把经过验证的范式在生成前注入上下文，能把"自由发挥"变成"模仿范式"，
直接提升一次通过率、降低重试成本。**一次通过率是本项目的北极星指标。**

为什么起步不用向量库：
* 语料规模小（几十到几百条人工精选范例），关键词/标签检索已经够用；
* 引入 embedding 就意味着额外的模型调用、额外的存储、额外的失败点；
* 过早优化检索质量，不如先把语料质量和数量做起来。

**升级路径**（阶段五）：pgvector + embedding 检索。接口保持不变，
只替换 :class:`FewShotRetriever` 的实现即可。
"""

from .store import (
    FewShot,
    FewShotRetriever,
    JsonCorpusRetriever,
    build_retriever,
    load_corpus,
)

__all__ = [
    "FewShot",
    "FewShotRetriever",
    "JsonCorpusRetriever",
    "build_retriever",
    "load_corpus",
]

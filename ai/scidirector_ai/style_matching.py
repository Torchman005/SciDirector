"""Offline, explainable style retrieval against the installed catalog."""
from __future__ import annotations

import math
import re
from collections import Counter

STOP_WORDS = set('我们 一个 一台 一张 一组 一段 这个 那个 这些 那些 它们 什么 为什么 如何 可以 通过 逐步 讲解 介绍 展示 然后 最后 需要 使用 内容 进行 以及 一种 其中 这里 这样 看到 得到 结果 过程 不同 本次 来看 看看'.split())


def tokens(text: str) -> set[str]:
    result = set(re.findall(r'[a-z][a-z0-9_-]{2,}', text.lower()))
    for word in re.findall(r'[\u3400-\u9fff]+', text):
        result.update(word[i:i + 2] for i in range(len(word) - 1))
    return {token for token in result if token not in STOP_WORDS and not any(c in token for c in '的了呢吗')}


def recommend_style(script: str, catalog: dict) -> dict:
    if not 20 <= len(script.strip()) <= 30000:
        raise ValueError('脚本需为 20～30000 字')
    entries = catalog['entries']
    query = tokens(script)
    fields = ['fit', 'keywords', 'name', 'look']
    documents = [{key: tokens(str(entry.get(key) or '')) for key in fields} for entry in entries]
    frequency = Counter(token for doc in documents for token in set().union(*doc.values()))
    scored = []
    weights = {'fit': 3, 'keywords': 2.5, 'name': 1.5, 'look': 1}
    for entry, doc in zip(entries, documents):
        matched = query & set().union(*doc.values())
        score = sum(math.log(1 + len(entries) / frequency[t]) *
                    max(weights[key] for key in fields if t in doc[key]) for t in matched)
        # Mild normalization: a one-word entry must not beat two specific facts
        # simply because the richer source also describes its appearance.
        score /= max(1, len(set().union(*doc.values()))) ** .15
        if score > 0:
            scored.append((score, entry['id'], entry, matched))
    scored.sort(key=lambda row: (-row[0], row[1]))
    base = {'method': 'catalog_keyword_match', 'library_version': catalog['library_version']}
    if not scored:
        return {**base, 'style_id': '', 'reason': '脚本与画风库暂无明确匹配，请手动选择。', 'alternatives': []}
    _, style_id, entry, matched = scored[0]
    ordered = sorted(matched, key=lambda t: (frequency[t], t))[:5]
    return {**base, 'style_id': style_id, 'reason': '匹配脚本用词：' + '、'.join(ordered),
            'name': entry['name'], 'matched_terms': ordered,
            'alternatives': [{'style_id': row[1], 'name': row[2]['name']} for row in scored[1:4]]}
